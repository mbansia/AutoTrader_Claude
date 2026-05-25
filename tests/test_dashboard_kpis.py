"""Dashboard KPI computation — equity / free_deployable / net_injected
with the v1.3 → v1.5 column-name drift.

A legacy v1.3 deploy wrote BalanceSnapshot rows with `spot_usdt`,
`futures_usdt`, `total_usdt`, `source`. After cutover to v1.5, those
rows are still in the DB but my v1.5 model added `*_free_usdt`,
`*_equity_usdt`, `mode` with default 0.0 / 'paper'. The dashboard reads
must show the legacy values to the operator instead of zeros.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'dash.db'}")
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "t")
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("BOT_WORKER_ENABLED", "0")
    import state.db as db_mod
    importlib.reload(db_mod)
    from web.app import create_app
    return TestClient(create_app())


def _insert_legacy_snapshot(db_path: str, *, source: str, spot_usdt: float,
                            futures_usdt: float, total_usdt: float) -> None:
    """Simulate a v1.3 row: legacy columns populated, v1.5 columns at default."""
    from sqlalchemy import create_engine, text
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO balance_snapshots "
            "(mode, exchange, ts, spot_free_usdt, futures_free_usdt, total_equity_usdt, "
            " source, spot_usdt, futures_usdt, total_usdt) VALUES "
            "('paper', 'binance', :ts, 0.0, 0.0, 0.0, :source, :spot, :fut, :total)"
        ), {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": source, "spot": spot_usdt, "fut": futures_usdt, "total": total_usdt,
        })


def test_dashboard_reads_legacy_snapshot_columns(client, tmp_path):
    """v1.3 row with `source='live'` + `total_usdt=$523.45` should surface
    on the LIVE dashboard even though `mode='paper'` and `total_equity_usdt=0`."""
    # Boot the app first so the DB + schema exist.
    client.get("/health")
    db_path = str(tmp_path / "dash.db")
    _insert_legacy_snapshot(
        db_path, source="live",
        spot_usdt=320.0, futures_usdt=200.0, total_usdt=523.45,
    )
    # Toggle the session cookie to live.
    r = client.post("/view/live", auth=("admin", "pw"), follow_redirects=False)
    assert r.status_code == 303

    r = client.get("/dashboard", auth=("admin", "pw"))
    assert r.status_code == 200
    body = r.text
    assert "$523.45" in body, "Current equity should fall back to legacy total_usdt"
    assert "$520.00" in body, "Free deployable should sum legacy spot_usdt + futures_usdt"


def test_dashboard_prefers_v15_columns_when_present(client, tmp_path):
    """A row with BOTH legacy + v1.5 columns populated should show the v1.5 value."""
    client.get("/health")
    db_path = str(tmp_path / "dash.db")
    from sqlalchemy import create_engine, text
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO balance_snapshots "
            "(mode, exchange, ts, spot_free_usdt, futures_free_usdt, total_equity_usdt, "
            " source, spot_usdt, futures_usdt, total_usdt) VALUES "
            "('live', 'binance', :ts, 100.0, 200.0, 999.99, 'live', 50.0, 50.0, 88.0)"
        ), {"ts": datetime.now(timezone.utc).isoformat()})
    client.post("/view/live", auth=("admin", "pw"), follow_redirects=False)
    r = client.get("/dashboard", auth=("admin", "pw"))
    body = r.text
    assert "$999.99" in body, "v1.5 total_equity_usdt should win over legacy total_usdt"
    assert "$300.00" in body, "v1.5 spot_free + futures_free should win"


def test_dashboard_uses_live_gateway_when_registered(client, tmp_path, monkeypatch):
    """When a live gateway is registered in core.config, the dashboard
    should aggregate balances live (not from the snapshot table)."""
    client.get("/health")
    from core.config import register_gateway, clear_registry
    from core.types import WalletBalance
    from gateways.paper import InMemoryGateway

    # Snapshot says $100; live gateway says $750. Dashboard should show $750.
    db_path = str(tmp_path / "dash.db")
    _insert_legacy_snapshot(db_path, source="live", spot_usdt=50, futures_usdt=50, total_usdt=100)

    clear_registry()
    gw = InMemoryGateway(exchange_id="binance")
    gw.balances["spot"]["USDT"] = WalletBalance(400.0, 400.0)
    gw.balances["futures"]["USDT"] = WalletBalance(350.0, 350.0)
    register_gateway("live", "binance", gw)
    try:
        client.post("/view/live", auth=("admin", "pw"), follow_redirects=False)
        r = client.get("/dashboard", auth=("admin", "pw"))
        body = r.text
        assert "$750.00" in body, "should aggregate live: 400 spot + 350 futures = 750"
        assert "live aggregate" in body, "equity_source meta should show 'live aggregate'"
    finally:
        clear_registry()


def test_dashboard_paper_mode_zero_when_empty(client):
    """Empty DB in paper mode shows $0.00 across the board (no fallback path)."""
    r = client.get("/dashboard", auth=("admin", "pw"))
    assert r.status_code == 200
    body = r.text
    # All KPIs should be $0.00 with no live data and no snapshot rows.
    assert body.count("$0.00") >= 4
