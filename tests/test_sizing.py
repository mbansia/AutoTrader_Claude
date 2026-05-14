"""Closed-form sizing tests. §3.1 math (v1.4)."""

from __future__ import annotations

from datetime import timedelta

from core.sizing import (
    size_buy_spot_price_dependent,
    size_entry,
    size_perp_flat_cap,
    size_sell_spot_flat_cap,
    snapshots_co_temporal,
)
from core.types import BookLevel, BookSnapshot, utcnow


def _ts(skew_ms: int = 0):
    return utcnow() + timedelta(milliseconds=skew_ms)


def _book(symbol, mid, asks, bids, ts=None):
    a = [BookLevel(price=p, depth=d) for p, d in asks]
    b = [BookLevel(price=p, depth=d) for p, d in bids]
    return BookSnapshot(symbol=symbol, bids=b, asks=a, mid_price=mid, taken_at=ts or _ts())


# ─── buy-spot (price-dependent) ────────────────────────────────────────────


def test_buy_spot_binding_at_book_depth():
    """Wallet is huge → binding constraint is book depth."""
    asks = [BookLevel(100.0, 1.0), BookLevel(101.0, 2.0), BookLevel(102.0, 5.0)]
    r = size_buy_spot_price_dependent(asks, wallet_quote_free=10_000_000, safety=1.0)
    assert r.qty == 1.0 + 2.0 + 5.0
    # vwap up to 8.0 depth
    expected_vwap = (1 * 100 + 2 * 101 + 5 * 102) / 8.0
    assert abs(r.avg_price - expected_vwap) < 1e-9


def test_buy_spot_binding_at_wallet():
    """Wallet is the binding constraint → can fund up to budget/price at
    each level; the optimum is where cum_qty just meets max_fund."""
    asks = [BookLevel(100.0, 10.0), BookLevel(110.0, 10.0)]
    # At level 0: cum=10, max_fund = 500/100 = 5.0; feasible = 5.0
    # At level 1: cum=20, max_fund = 500/110 ≈ 4.54; feasible = 4.54
    # argmax = level 0 with feasible 5.0
    r = size_buy_spot_price_dependent(asks, wallet_quote_free=500.0, safety=1.0)
    assert abs(r.qty - 5.0) < 1e-9


def test_buy_spot_empty_wallet():
    r = size_buy_spot_price_dependent(
        [BookLevel(100.0, 1.0)], wallet_quote_free=0.0, safety=1.0
    )
    assert r.qty == 0.0
    assert r.rejection == "empty_wallet_or_book"


# ─── sell-spot (flat cap on base balance) ──────────────────────────────────


def test_sell_spot_capped_by_balance():
    bids = [BookLevel(99.0, 10.0)]
    r = size_sell_spot_flat_cap(bids, base_balance_free=3.0, safety=1.0)
    assert r.qty == 3.0
    assert r.avg_price == 99.0


def test_sell_spot_capped_by_book_depth():
    bids = [BookLevel(99.0, 2.0)]
    r = size_sell_spot_flat_cap(bids, base_balance_free=10.0, safety=1.0)
    assert r.qty == 2.0


# ─── perp (flat cap on margin from mark) ───────────────────────────────────


def test_perp_capped_by_margin():
    bids = [BookLevel(100.0, 100.0)]
    r = size_perp_flat_cap(
        bids, mark_price=100.0, wallet_quote_free=500.0, safety=1.0, leverage=1.0
    )
    # margin = 500 × 1 × 1 / 100 = 5.0
    assert r.qty == 5.0


def test_perp_capped_by_depth():
    bids = [BookLevel(100.0, 2.0)]
    r = size_perp_flat_cap(
        bids, mark_price=100.0, wallet_quote_free=10_000.0, safety=1.0, leverage=1.0
    )
    assert r.qty == 2.0


def test_perp_leverage_multiplies():
    bids = [BookLevel(100.0, 100.0)]
    r = size_perp_flat_cap(
        bids, mark_price=100.0, wallet_quote_free=500.0, safety=1.0, leverage=2.0
    )
    # margin = 500 × 1 × 2 / 100 = 10.0
    assert r.qty == 10.0


# ─── end-to-end entry sizing ───────────────────────────────────────────────


def test_size_entry_returns_zero_on_empty_book():
    sb = _book("X/USDT", 100.0, asks=[], bids=[])
    pb = _book("X/USDT:USDT", 100.0, asks=[], bids=[])
    r = size_entry(
        spot_book=sb,
        perp_book=pb,
        spot_wallet_quote_free=10_000,
        perp_wallet_quote_free=10_000,
        leverage=1.0,
        safety=0.99,
        sub_target=0.75,
        operator_cap_qty=1e9,
        spot_lot_step=0.001,
        perp_lot_step=0.001,
    )
    assert r.target_qty == 0.0
    assert r.rejection_reason == "empty_book"


def test_size_entry_applies_sub_target():
    """sub_target=0.75 should shrink the result to ~75% of the binding qty."""
    sb = _book("X/USDT", 100.0, asks=[(100.0, 10.0)], bids=[(99.0, 10.0)])
    pb = _book("X/USDT:USDT", 100.0, asks=[(100.0, 10.0)], bids=[(100.0, 10.0)])
    r = size_entry(
        spot_book=sb,
        perp_book=pb,
        spot_wallet_quote_free=1_000_000,
        perp_wallet_quote_free=1_000_000,
        leverage=1.0,
        safety=1.0,
        sub_target=0.75,
        operator_cap_qty=10.0,
        spot_lot_step=0.001,
        perp_lot_step=0.001,
    )
    # Binding qty = 10 (book depth), × 0.75 = 7.5, floored to lot step.
    assert abs(r.target_qty - 7.5) < 0.001


def test_size_entry_below_lot_step():
    """Tiny budget → target_qty rounds to 0 lot steps."""
    sb = _book("X/USDT", 100.0, asks=[(100.0, 0.0005)], bids=[(99.0, 0.0005)])
    pb = _book("X/USDT:USDT", 100.0, asks=[(100.0, 0.0005)], bids=[(100.0, 0.0005)])
    r = size_entry(
        spot_book=sb,
        perp_book=pb,
        spot_wallet_quote_free=1_000,
        perp_wallet_quote_free=1_000,
        leverage=1.0,
        safety=1.0,
        sub_target=0.75,
        operator_cap_qty=1e9,
        spot_lot_step=0.001,  # min increment 0.001
        perp_lot_step=0.001,
    )
    assert r.target_qty == 0.0
    assert r.rejection_reason == "below_lot_step_after_sub_target"


def test_size_entry_operator_cap_binds():
    """Operator's max_position_pct can override book + wallet."""
    sb = _book("X/USDT", 100.0, asks=[(100.0, 100.0)], bids=[(99.0, 100.0)])
    pb = _book("X/USDT:USDT", 100.0, asks=[(100.0, 100.0)], bids=[(100.0, 100.0)])
    r = size_entry(
        spot_book=sb,
        perp_book=pb,
        spot_wallet_quote_free=1_000_000,
        perp_wallet_quote_free=1_000_000,
        leverage=1.0,
        safety=1.0,
        sub_target=0.75,
        operator_cap_qty=2.0,  # only allow 2 base units
        spot_lot_step=0.001,
        perp_lot_step=0.001,
    )
    # 2.0 × 0.75 = 1.5
    assert abs(r.target_qty - 1.5) < 0.001


# ─── snapshot atomicity (L42) ──────────────────────────────────────────────


def test_snapshots_co_temporal_within_tolerance():
    a = _book("A", 100.0, asks=[(100.0, 1.0)], bids=[(99.0, 1.0)], ts=_ts(0))
    b = _book("B", 100.0, asks=[(100.0, 1.0)], bids=[(99.0, 1.0)], ts=_ts(30))
    assert snapshots_co_temporal(a, b)


def test_snapshots_skew_rejected():
    a = _book("A", 100.0, asks=[(100.0, 1.0)], bids=[(99.0, 1.0)], ts=_ts(0))
    b = _book("B", 100.0, asks=[(100.0, 1.0)], bids=[(99.0, 1.0)], ts=_ts(200))
    assert not snapshots_co_temporal(a, b)
