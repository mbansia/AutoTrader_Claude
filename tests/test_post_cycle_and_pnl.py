"""Post-cycle BalanceSnapshot writing + PnL decomposition tests.

Two pieces:
  1. `loop.runner._write_balance_snapshot` — called per cycle, writes both
     v1.5 and legacy column names so the dashboard read works regardless
     of which generation of code populated the row.
  2. `web.routes.dashboard._realized_trade_pnl` + `_funding_income` —
     decomposition that exposes which component is driving Total PnL.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pnl.db'}")
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "t")
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("BOT_WORKER_ENABLED", "0")
    import state.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    return db_mod


def test_write_balance_snapshot_populates_both_column_sets(fresh_db):
    """The post-cycle snapshot writer fills BOTH v1.5 (`*_free_usdt`,
    `total_equity_usdt`, `mode`) AND legacy (`*_usdt`, `total_usdt`,
    `source`) columns so a stale-schema dashboard read still surfaces
    the row.
    """
    from core.types import WalletBalance
    from gateways.paper import InMemoryGateway
    from loop.runner import _write_balance_snapshot
    from state import models as m
    from state import session_scope

    gw = InMemoryGateway(exchange_id="binance")
    gw.balances["spot"]["USDT"] = WalletBalance(800.0, 800.0)
    gw.balances["futures"]["USDT"] = WalletBalance(200.0, 200.0)
    gw.balances["spot"]["USDC"] = WalletBalance(50.0, 50.0)
    gw.balances["futures"]["USDC"] = WalletBalance(0.0, 0.0)

    with session_scope() as session:
        _write_balance_snapshot(session, gw, "live")
        row = session.execute(
            __import__("sqlalchemy").select(m.BalanceSnapshot)
        ).scalar_one()
        snap = {
            "mode": row.mode,
            "spot_free_usdt": row.spot_free_usdt,
            "futures_free_usdt": row.futures_free_usdt,
            "total_equity_usdt": row.total_equity_usdt,
            "source": row.source,
            "spot_usdt": row.spot_usdt,
            "futures_usdt": row.futures_usdt,
            "total_usdt": row.total_usdt,
        }

    # v1.5 columns
    assert snap["mode"] == "live"
    assert snap["spot_free_usdt"] == 850.0  # USDT 800 + USDC 50
    assert snap["futures_free_usdt"] == 200.0
    assert snap["total_equity_usdt"] == 1050.0  # 850 spot + 200 fut
    # legacy back-compat
    assert snap["source"] == "live"
    assert snap["spot_usdt"] == 850.0
    assert snap["futures_usdt"] == 200.0
    assert snap["total_usdt"] == 1050.0


def test_write_balance_snapshot_swallows_gateway_failure(fresh_db, monkeypatch):
    """Balance fetch raises → snapshot writer logs + returns; cycle continues."""
    from gateways.paper import InMemoryGateway
    from loop.runner import _write_balance_snapshot
    from state import models as m
    from state import session_scope

    gw = InMemoryGateway(exchange_id="binance")

    def boom(*_args, **_kwargs):
        raise RuntimeError("venue offline")

    monkeypatch.setattr(gw, "fetch_balance", boom)

    with session_scope() as session:
        _write_balance_snapshot(session, gw, "live")  # must not raise
        count = session.execute(
            __import__("sqlalchemy").select(__import__("sqlalchemy").func.count(m.BalanceSnapshot.id))
        ).scalar_one()
    assert count == 0


def test_realized_trade_pnl_from_paired_legs(fresh_db):
    """A closed position with full trade history → realized P&L = sum of
    spot_leg PnL + perp_leg PnL.
    """
    from web.routes.dashboard import _realized_trade_pnl
    from state import models as m
    from state import session_scope

    with session_scope() as session:
        # Position: bought 1 BTC at spot=$50000, sold short perp at $50050.
        # Closed: sold spot at $51000, bought back perp at $51050.
        # Spot leg PnL: 1 × (51000 - 50000) = +$1000
        # Perp leg PnL: 1 × (50050 - 51050) = -$1000
        # Net: $0 (textbook delta-neutral). The bot earns from funding only.
        pos = m.Position(
            mode="live", exchange="binance",
            trade_type="binance_same_venue_funding_arb",
            status="closed",
            symbol="BTC", spot_symbol="BTC/USDT", perp_symbol="BTC/USDT:USDT",
            quote_currency="USDT", spot_quote_currency="USDT",
            quantity=1.0,
            spot_entry_price=50000.0, perp_entry_price=50050.0,
            opened_at=datetime.now(timezone.utc),
            closed_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        session.flush()
        # Entry trades (irrelevant to PnL — entry prices come from the position)
        session.add(m.Trade(
            mode="live", exchange="binance",
            trade_type="binance_same_venue_funding_arb",
            position_id=pos.id, symbol="BTC/USDT", venue="spot", side="buy",
            quantity=1.0, price=50000.0, fee=0.5,
        ))
        session.add(m.Trade(
            mode="live", exchange="binance",
            trade_type="binance_same_venue_funding_arb",
            position_id=pos.id, symbol="BTC/USDT:USDT", venue="futures", side="sell",
            quantity=1.0, price=50050.0, fee=0.5,
        ))
        # Exit trades
        session.add(m.Trade(
            mode="live", exchange="binance",
            trade_type="binance_same_venue_funding_arb",
            position_id=pos.id, symbol="BTC/USDT", venue="spot", side="sell",
            quantity=1.0, price=51000.0, fee=0.5,
        ))
        session.add(m.Trade(
            mode="live", exchange="binance",
            trade_type="binance_same_venue_funding_arb",
            position_id=pos.id, symbol="BTC/USDT:USDT", venue="futures", side="buy",
            quantity=1.0, price=51050.0, fee=0.5,
        ))
        session.flush()

        pnl = _realized_trade_pnl(session, "live")
    # Textbook delta-neutral: zero realized PnL on the legs themselves.
    assert abs(pnl - 0.0) < 1e-6


def test_realized_trade_pnl_adverse_basis(fresh_db):
    """If basis widens against the bot during the hold:
        entry: spot 50000, perp 50050 (basis +50)
        exit:  spot 51000, perp 51200 (basis +200)
    Spot leg: +1000.  Perp leg: -1150.  Net: -150 (bot lost on basis).
    """
    from web.routes.dashboard import _realized_trade_pnl
    from state import models as m
    from state import session_scope

    with session_scope() as session:
        pos = m.Position(
            mode="live", exchange="binance",
            trade_type="binance_same_venue_funding_arb", status="closed",
            symbol="BTC", spot_symbol="BTC/USDT", perp_symbol="BTC/USDT:USDT",
            quote_currency="USDT", spot_quote_currency="USDT", quantity=1.0,
            spot_entry_price=50000.0, perp_entry_price=50050.0,
            opened_at=datetime.now(timezone.utc),
            closed_at=datetime.now(timezone.utc),
        )
        session.add(pos)
        session.flush()
        session.add(m.Trade(
            mode="live", exchange="binance",
            trade_type="binance_same_venue_funding_arb",
            position_id=pos.id, symbol="BTC/USDT", venue="spot", side="sell",
            quantity=1.0, price=51000.0, fee=0.5,
        ))
        session.add(m.Trade(
            mode="live", exchange="binance",
            trade_type="binance_same_venue_funding_arb",
            position_id=pos.id, symbol="BTC/USDT:USDT", venue="futures", side="buy",
            quantity=1.0, price=51200.0, fee=0.5,
        ))
        session.flush()
        pnl = _realized_trade_pnl(session, "live")
    assert abs(pnl - -150.0) < 1e-6


def test_funding_income_sums_across_positions(fresh_db):
    """funding_income_accrued sums across both open and closed positions."""
    from web.routes.dashboard import _funding_income
    from state import models as m
    from state import session_scope

    with session_scope() as session:
        session.add(m.Position(
            mode="live", exchange="binance",
            trade_type="binance_same_venue_funding_arb", status="open",
            symbol="BTC", spot_symbol="BTC/USDT", perp_symbol="BTC/USDT:USDT",
            quote_currency="USDT", spot_quote_currency="USDT", quantity=1.0,
            spot_entry_price=50000.0, perp_entry_price=50050.0,
            opened_at=datetime.now(timezone.utc),
            funding_income_accrued=12.34,
        ))
        session.add(m.Position(
            mode="live", exchange="kucoin",
            trade_type="kucoin_same_venue_funding_arb", status="closed",
            symbol="ETH", spot_symbol="ETH/USDT", perp_symbol="ETH/USDT:USDT",
            quote_currency="USDT", spot_quote_currency="USDT", quantity=2.0,
            spot_entry_price=3000.0, perp_entry_price=3010.0,
            opened_at=datetime.now(timezone.utc),
            closed_at=datetime.now(timezone.utc),
            funding_income_accrued=5.66,
        ))
        # A paper-mode row must NOT count.
        session.add(m.Position(
            mode="paper", exchange="binance",
            trade_type="binance_same_venue_funding_arb", status="closed",
            symbol="XYZ", spot_symbol="XYZ/USDT", perp_symbol="XYZ/USDT:USDT",
            quote_currency="USDT", spot_quote_currency="USDT", quantity=1.0,
            spot_entry_price=1.0, perp_entry_price=1.0,
            opened_at=datetime.now(timezone.utc),
            closed_at=datetime.now(timezone.utc),
            funding_income_accrued=999.99,  # would skew if mode filter is broken
        ))
        session.flush()
        live_total = _funding_income(session, "live")
        paper_total = _funding_income(session, "paper")
    assert abs(live_total - 18.0) < 1e-6
    assert abs(paper_total - 999.99) < 1e-6


def test_dashboard_renders_pnl_breakdown(tmp_path, monkeypatch):
    """End-to-end: dashboard HTML shows the trade/funding sub-line under Total PnL."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'render.db'}")
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "t")
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("BOT_WORKER_ENABLED", "0")
    import state.db as db_mod
    importlib.reload(db_mod)
    from web.app import create_app
    c = TestClient(create_app())
    r = c.get("/dashboard", auth=("admin", "pw"))
    assert r.status_code == 200
    body = r.text
    assert "trade $0.00" in body
    assert "funding $0.00" in body
