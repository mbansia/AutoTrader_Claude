"""CTO-audit pass 1 follow-ups: tests for the high-severity issues caught
in the audit (deferral sign, single-leg orphan paths, phantom recovery).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.types import (
    BookLevel,
    BookSnapshot,
    FundingInfo,
    MergedConfig,
    PositionStatus,
    WalletBalance,
)
from loop.cycle import run_cycle
from state import models as m
from state.repository import (
    get_or_create_mode_state,
    get_or_seed_per_strategy_config,
)


def _bootstrap(session, mode="paper"):
    get_or_create_mode_state(session, mode)
    get_or_seed_per_strategy_config(session, "binance_same_venue_funding_arb")


def _seed_basic_pair(gateway, *, funding_rate=0.005, spot_mid=100.0, perp_mid=100.10):
    """Same as test_cycle's fixture but exposed here for reuse."""
    now = datetime.now(timezone.utc)
    sp = "ABC/USDT"; pp = "ABC/USDT:USDT"
    gateway.books[sp] = BookSnapshot(
        sp,
        [BookLevel(spot_mid - 0.05, 100.0)],
        [BookLevel(spot_mid, 100.0)],
        spot_mid,
        now,
    )
    gateway.books[pp] = BookSnapshot(
        pp,
        [BookLevel(perp_mid - 0.05, 100.0)],
        [BookLevel(perp_mid, 100.0)],
        perp_mid,
        now,
    )
    gateway.funding[pp] = FundingInfo(
        symbol=pp,
        predicted_rate=funding_rate,
        interval_hours=4.0,
        next_funding_ts=now + timedelta(hours=4),
    )
    gateway.balances["spot"]["USDT"] = WalletBalance(10_000, 10_000)
    gateway.balances["futures"]["USDT"] = WalletBalance(10_000, 10_000)
    gateway.lots[sp] = 0.001
    gateway.lots[pp] = 0.001


# ─── Deferral sign correctness (high-severity audit fix) ───────────────────


def test_voluntary_exit_defers_on_positive_basis(session, gateway):
    """For long-spot/short-perp, POSITIVE live_basis is adverse for closing
    (perp expensive vs spot). Deferral fires."""
    _bootstrap(session)
    _seed_basic_pair(gateway)
    cfg = MergedConfig(max_exit_basis_bps=5.0, basis_dislocation_exit_bps=100.0)
    run_cycle(session=session, gateway=gateway, mode="paper", config=cfg)
    session.commit()
    assert session.query(m.Position).filter_by(status="open").count() == 1

    # Drop funding → forward profit gate would fire voluntary exit; but
    # live_basis is +15 bps (adverse for closing) → defer.
    pp = "ABC/USDT:USDT"
    gateway.funding[pp] = FundingInfo(pp, 0.0, 4.0, datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    gateway.books["ABC/USDT"] = BookSnapshot(
        "ABC/USDT", [BookLevel(99.95, 100)], [BookLevel(100.0, 100)], 100.0, now
    )
    gateway.books[pp] = BookSnapshot(
        pp, [BookLevel(100.10, 100)], [BookLevel(100.15, 100)], 100.15, now
    )
    gateway.seen_client_order_ids.clear()
    res = run_cycle(session=session, gateway=gateway, mode="paper", config=cfg)
    session.commit()
    # Should defer, not close.
    assert any("defer_exit" in a for a in res.actions)
    assert session.query(m.Position).filter_by(status="open").count() == 1


def test_voluntary_exit_fires_on_favourable_negative_basis(session, gateway):
    """Symmetric: NEGATIVE live_basis is favourable for closing → no deferral."""
    _bootstrap(session)
    _seed_basic_pair(gateway)
    cfg = MergedConfig(max_exit_basis_bps=5.0, basis_dislocation_exit_bps=200.0)
    run_cycle(session=session, gateway=gateway, mode="paper", config=cfg)
    session.commit()

    pp = "ABC/USDT:USDT"
    gateway.funding[pp] = FundingInfo(pp, 0.0, 4.0, datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)
    # Spot mid 100, perp mid 99.9 → live_basis = -10 bps (favourable).
    gateway.books["ABC/USDT"] = BookSnapshot(
        "ABC/USDT", [BookLevel(99.95, 100)], [BookLevel(100.0, 100)], 100.0, now
    )
    gateway.books[pp] = BookSnapshot(
        pp, [BookLevel(99.85, 100)], [BookLevel(99.90, 100)], 99.90, now
    )
    gateway.seen_client_order_ids.clear()
    res = run_cycle(session=session, gateway=gateway, mode="paper", config=cfg)
    session.commit()
    closed = session.query(m.Position).filter_by(status="closed").count()
    assert closed == 1


# ─── Single-leg orphan paths ──────────────────────────────────────────────


def test_single_leg_orphan_rollback_succeeds(session, gateway, monkeypatch):
    """One leg fills, the other rejects (FOK), rollback succeeds → no naked row.
    Forced via monkey-patching the perp side to reject regardless of book.
    """
    _bootstrap(session)
    _seed_basic_pair(gateway)
    original = gateway.place_market_fok
    perp_seen = {"count": 0}

    def selective_fok(symbol, leg, side, qty, coid):
        # Reject the FIRST perp call (the entry leg); accept the SECOND perp
        # call (the rollback if needed). Also accept any spot call.
        if leg == "futures":
            perp_seen["count"] += 1
            if perp_seen["count"] == 1:
                from core.types import FillResult
                return FillResult(
                    symbol=symbol, venue_leg=leg, side=side, qty=0.0,
                    avg_price=0.0, fee_quote=0.0, accepted=False,
                    error="fok_rejected", client_order_id=coid,
                )
        return original(symbol, leg, side, qty, coid)

    monkeypatch.setattr(gateway, "place_market_fok", selective_fok)

    cfg = MergedConfig(sub_target_sizing_factor=1.0, max_position_pct=0.5)
    res = run_cycle(session=session, gateway=gateway, mode="paper", config=cfg)
    session.commit()
    # No naked row persisted (spot was rolled back).
    assert session.query(m.Position).count() == 0
    rejs = session.query(m.RejectedCandidate).all()
    assert any("single_leg_orphan" in r.reason for r in rejs)


# ─── Phantom-leg recovery (Phase A symmetry) ──────────────────────────────


def test_naked_perp_recovery_in_live_mode(session, gateway):
    """A naked_perp row should attempt buy-spot hedge if forward profit OK,
    else buy back perp. This exercises the new symmetric recovery path."""
    _bootstrap(session, mode="live")
    _seed_basic_pair(gateway)
    pp = "ABC/USDT:USDT"
    # Persist a naked_perp directly:
    pos = m.Position(
        mode="live",
        exchange="binance",
        trade_type="binance_same_venue_funding_arb",
        status=PositionStatus.NAKED_PERP.value,
        symbol="ABC",
        spot_symbol="ABC/USDT",
        perp_symbol=pp,
        quote_currency="USDT",
        spot_quote_currency="USDT",
        quantity=0.1,
        spot_entry_price=0.0,
        perp_entry_price=100.0,
        opened_at=datetime.now(timezone.utc),
        last_funding_accrual_ts=datetime.now(timezone.utc),
    )
    session.add(pos)
    # Underlying perp short is on the venue (so stale reconcile won't fire).
    gateway.open_perps = [(pp, -0.1)]
    session.commit()

    cfg = MergedConfig()
    res = run_cycle(session=session, gateway=gateway, mode="live", config=cfg)
    session.commit()
    # Either hedge succeeded (now OPEN) or flat-closed (now CLOSED).
    final = session.query(m.Position).first()
    assert final.status in {PositionStatus.OPEN.value, PositionStatus.CLOSED.value}
    assert any(
        a.startswith("phantom_perp_rescued") or a.startswith("phantom_perp_closed")
        for a in res.actions
    )


def test_naked_spot_stale_reconciled_when_balance_gone(session, gateway):
    """L17: a naked_spot whose underlying balance disappeared from the wallet
    is auto-marked closed in Phase A stale-reconciliation."""
    _bootstrap(session, mode="live")
    _seed_basic_pair(gateway)
    pos = m.Position(
        mode="live",
        exchange="binance",
        trade_type="binance_same_venue_funding_arb",
        status=PositionStatus.NAKED_SPOT.value,
        symbol="ABC",
        spot_symbol="ABC/USDT",
        perp_symbol="ABC/USDT:USDT",
        quote_currency="USDT",
        spot_quote_currency="USDT",
        quantity=0.1,
        spot_entry_price=100.0,
        perp_entry_price=0.0,
        opened_at=datetime.now(timezone.utc),
        last_funding_accrual_ts=datetime.now(timezone.utc),
    )
    session.add(pos)
    # No ABC in the wallet → stale.
    session.commit()

    cfg = MergedConfig()
    res = run_cycle(session=session, gateway=gateway, mode="live", config=cfg)
    session.commit()
    assert session.query(m.Position).filter_by(status="closed").count() == 1
    assert any("stale_reconciled" in a for a in res.actions)


# ─── Executor exception handling ──────────────────────────────────────────


def test_single_leg_orphan_rollback_fails_persists_naked(session, gateway, monkeypatch):
    """Forced both-sides failure on the perp + on the rollback: spot leg fills
    on entry, perp leg rejects, then rollback (sell-back of spot) ALSO rejects
    → naked_spot row persisted; phantom-recovery picks it up next cycle.
    """
    _bootstrap(session)
    _seed_basic_pair(gateway)
    original = gateway.place_market_fok
    perp_attempt = {"count": 0}

    def force_perp_and_rollback_fail(symbol, leg, side, qty, coid):
        # First perp call (entry) rejects.
        if leg == "futures":
            perp_attempt["count"] += 1
            from core.types import FillResult
            return FillResult(
                symbol=symbol, venue_leg=leg, side=side, qty=0.0,
                avg_price=0.0, fee_quote=0.0, accepted=False,
                error="fok_rejected", client_order_id=coid,
            )
        # Spot SELL (the rollback) also rejects — book is "thin on the way back".
        if leg == "spot" and side == "sell":
            from core.types import FillResult
            return FillResult(
                symbol=symbol, venue_leg=leg, side=side, qty=0.0,
                avg_price=0.0, fee_quote=0.0, accepted=False,
                error="fok_rejected_rollback", client_order_id=coid,
            )
        return original(symbol, leg, side, qty, coid)

    monkeypatch.setattr(gateway, "place_market_fok", force_perp_and_rollback_fail)
    cfg = MergedConfig(sub_target_sizing_factor=1.0, max_position_pct=0.5)
    res = run_cycle(session=session, gateway=gateway, mode="paper", config=cfg)
    session.commit()
    nakeds = session.query(m.Position).filter(m.Position.status == "naked_spot").all()
    assert len(nakeds) == 1, "rollback failure must persist a naked_spot row"
    assert any("naked_persisted" in a for a in res.actions)


def test_executor_per_leg_exception_does_not_crash_cycle(session, gateway, monkeypatch):
    """A gateway exception inside a single-leg call must NOT crash the cycle;
    the executor catches it and treats it as a non-accepted FillResult."""
    _bootstrap(session)
    _seed_basic_pair(gateway)

    original = gateway.place_market_fok

    def flaky(symbol, leg, side, qty, coid):
        if leg == "futures":
            raise RuntimeError("transient_network_blip")
        return original(symbol, leg, side, qty, coid)

    monkeypatch.setattr(gateway, "place_market_fok", flaky)
    cfg = MergedConfig()
    res = run_cycle(session=session, gateway=gateway, mode="paper", config=cfg)
    session.commit()
    # The cycle DID complete (no res.error). A rejection or naked row was
    # persisted depending on whether the spot leg filled and got rolled back.
    assert res.error is None
