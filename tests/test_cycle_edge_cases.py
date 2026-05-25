"""Targeted edge-case tests to cover previously uncovered branches in loop/cycle.py:

  - Entry snapshot_skew rejection (lines 527-537)
  - Entry zero-qty sizing rejection (lines 557-567)
  - Entry both-legs FOK rejected → EntryOutcome.REJECTED (lines 676-686)
  - Entry perp-only single-leg orphan naked (lines 709-711)
  - Exit snapshot_skew warning (lines 439-447)
  - Stuck-close: both close-legs FOK rejected (lines 856-868)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.types import (
    BookLevel,
    BookSnapshot,
    FillResult,
    FundingInfo,
    MergedConfig,
    WalletBalance,
)
from loop.cycle import run_cycle
from state import models as m
from state.repository import (
    get_or_create_mode_state,
    get_or_seed_per_strategy_config,
    recent_events,
)

_SP = "ABC/USDT"
_PP = "ABC/USDT:USDT"


def _bootstrap(session, mode: str = "paper") -> None:
    get_or_create_mode_state(session, mode)
    get_or_seed_per_strategy_config(session, "binance_same_venue_funding_arb")


def _seed_pair(
    gateway,
    *,
    spot_mid: float = 100.0,
    perp_mid: float = 100.10,
    skew_ms: int = 0,
) -> None:
    """Fat books, fat wallets, high funding. Optional taken_at skew on perp book."""
    now = datetime.now(timezone.utc)
    perp_ts = now + timedelta(milliseconds=skew_ms)
    gateway.books[_SP] = BookSnapshot(
        _SP,
        [BookLevel(spot_mid - 0.05, 500.0)],
        [BookLevel(spot_mid, 500.0)],
        spot_mid,
        now,
    )
    gateway.books[_PP] = BookSnapshot(
        _PP,
        [BookLevel(perp_mid - 0.05, 500.0)],
        [BookLevel(perp_mid, 500.0)],
        perp_mid,
        perp_ts,
    )
    gateway.funding[_PP] = FundingInfo(
        symbol=_PP,
        predicted_rate=0.005,
        interval_hours=4.0,
        next_funding_ts=now + timedelta(hours=4),
    )
    gateway.balances["spot"]["USDT"] = WalletBalance(10_000.0, 10_000.0)
    gateway.balances["futures"]["USDT"] = WalletBalance(10_000.0, 10_000.0)
    gateway.lots[_SP] = 0.001
    gateway.lots[_PP] = 0.001
    gateway.ticks[_SP] = 0.01
    gateway.ticks[_PP] = 0.01


def _reject(symbol: str, leg: str, side: str, coid: str) -> FillResult:
    return FillResult(
        symbol=symbol,
        venue_leg=leg,
        side=side,
        qty=0.0,
        avg_price=0.0,
        fee_quote=0.0,
        accepted=False,
        error="fok_rejected",
        client_order_id=coid,
    )


# ─── Entry edge cases ──────────────────────────────────────────────────────

def test_entry_snapshot_skew_rejection(session, gateway):
    """Books >50 ms apart → snapshot_skew rejection row inserted (lines 527-537)."""
    _bootstrap(session)
    _seed_pair(gateway, skew_ms=100)  # 100 ms > 50 ms co-temporal threshold
    res = run_cycle(session=session, gateway=gateway, mode="paper", config=MergedConfig())
    assert any("snapshot_skew" in r for r in res.rejections)
    assert session.query(m.RejectedCandidate).filter_by(reason="snapshot_skew").count() == 1


def test_entry_zero_qty_rejection(session, gateway):
    """Wallet too small for any lot → sizing returns 0 qty, rejection row (lines 557-567)."""
    _bootstrap(session)
    _seed_pair(gateway)
    # Shrink wallets so operator_cap_qty floors to zero after lot-step rounding.
    gateway.balances["spot"]["USDT"] = WalletBalance(0.001, 0.001)
    gateway.balances["futures"]["USDT"] = WalletBalance(0.001, 0.001)
    res = run_cycle(session=session, gateway=gateway, mode="paper", config=MergedConfig())
    # Must have a rejection (sizing reason, not snapshot_skew).
    assert res.rejections
    assert not any("snapshot_skew" in r for r in res.rejections)
    rc = session.query(m.RejectedCandidate).one()
    assert "below_lot_step_after_sub_target" in rc.reason


def test_entry_both_fok_rejected(session, gateway):
    """Both legs FOK-rejected → EntryOutcome.REJECTED → fok_rejected row (lines 676-686)."""
    _bootstrap(session)
    _seed_pair(gateway)

    def _always_reject(symbol, leg, side, qty, coid):
        return _reject(symbol, leg, side, coid)

    gateway.place_market_fok = _always_reject
    res = run_cycle(session=session, gateway=gateway, mode="paper", config=MergedConfig())
    assert res.rejections
    assert any("fok_rejected" in r for r in res.rejections)
    assert session.query(m.RejectedCandidate).count() == 1


def test_entry_perp_only_naked(session, gateway):
    """Spot FOK rejects, perp fills, rollback rejects → naked_perp with perp_entry_price
    set; covers the SINGLE_LEG_ORPHAN_NAKED perp-only branch (lines 709-711)."""
    _bootstrap(session)
    _seed_pair(gateway)

    perp_fill_price = 100.10

    def _selective(symbol, leg, side, qty, coid):
        if leg == "spot":
            return _reject(symbol, leg, side, coid)
        if leg == "futures" and side == "sell":
            # Perp entry: accept, simulate fill at mid.
            return FillResult(
                symbol=symbol,
                venue_leg="futures",
                side="sell",
                qty=qty,
                avg_price=perp_fill_price,
                fee_quote=0.0,
                accepted=True,
                error="",
                client_order_id=coid,
            )
        # Rollback buy → reject → SINGLE_LEG_ORPHAN_NAKED.
        return _reject(symbol, leg, side, coid)

    gateway.place_market_fok = _selective
    run_cycle(session=session, gateway=gateway, mode="paper", config=MergedConfig())
    session.commit()

    pos = session.query(m.Position).one()
    assert pos.status == "naked_perp"
    assert pos.perp_entry_price == pytest.approx(perp_fill_price)
    assert pos.spot_entry_price == 0.0


# ─── Exit edge cases ───────────────────────────────────────────────────────

def test_exit_snapshot_skew_warn(session, gateway):
    """Skewed exit books abort the exit evaluation and log WARN (lines 439-447)."""
    _bootstrap(session)
    _seed_pair(gateway)
    run_cycle(session=session, gateway=gateway, mode="paper", config=MergedConfig())
    session.commit()
    assert session.query(m.Position).filter_by(status="open").count() == 1

    # Replace books with co-temporal pair where funding=0 (voluntary exit would fire),
    # but inject a 200 ms perp skew so the co-temporal guard fires first.
    now = datetime.now(timezone.utc)
    gateway.funding[_PP] = FundingInfo(_PP, 0.0, 4.0, now + timedelta(hours=4))
    gateway.books[_SP] = BookSnapshot(
        _SP,
        [BookLevel(99.95, 500.0)],
        [BookLevel(100.0, 500.0)],
        100.0,
        now,
    )
    gateway.books[_PP] = BookSnapshot(
        _PP,
        [BookLevel(99.95, 500.0)],
        [BookLevel(100.0, 500.0)],
        100.0,
        now + timedelta(milliseconds=200),
    )
    gateway.seen_client_order_ids.clear()
    run_cycle(session=session, gateway=gateway, mode="paper", config=MergedConfig())
    session.commit()

    # Position must still be open (exit was aborted).
    assert session.query(m.Position).filter_by(status="open").count() == 1
    warns = [
        e for e in recent_events(session, hours=1, levels=["WARN"])
        if "snapshot_skew" in e.message
    ]
    assert len(warns) >= 1


def test_stuck_close(session, gateway):
    """Both close-legs FOK rejected → last_close_error set + ERROR event (lines 856-868)."""
    _bootstrap(session)
    _seed_pair(gateway)
    # Very tight stop-loss so a small perp move triggers it.
    cfg = MergedConfig(stop_loss_pct=-0.001)
    run_cycle(session=session, gateway=gateway, mode="paper", config=cfg)
    session.commit()
    assert session.query(m.Position).filter_by(status="open").count() == 1

    # Move perp mark way up → short-perp loss → stop-loss fires on next cycle.
    now = datetime.now(timezone.utc)
    gateway.books[_SP] = BookSnapshot(
        _SP, [BookLevel(99.95, 500.0)], [BookLevel(100.0, 500.0)], 100.0, now
    )
    gateway.books[_PP] = BookSnapshot(
        _PP, [BookLevel(120.0, 500.0)], [BookLevel(120.05, 500.0)], 120.0, now
    )
    gateway.seen_client_order_ids.clear()

    # Reject all close-leg fills so the position gets stuck.
    def _always_reject(symbol, leg, side, qty, coid):
        return _reject(symbol, leg, side, coid)

    gateway.place_market_fok = _always_reject
    res = run_cycle(session=session, gateway=gateway, mode="paper", config=cfg)
    session.commit()

    pos = session.query(m.Position).one()
    assert pos.status == "open"  # stuck: not closed
    assert pos.last_close_error and "close_failed" in pos.last_close_error
    assert any("close_failed" in a for a in res.actions)
    error_events = [
        e for e in recent_events(session, hours=1, levels=["ERROR"])
        if "close_failed" in e.message
    ]
    assert len(error_events) >= 1
