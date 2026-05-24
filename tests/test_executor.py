"""Unit tests for loop/executor.py.

Covers the three previously-untested paths:
  - REJECTED outcome (both legs not accepted) → also exercises _combine_reasons
  - _combine_reasons with no error fields → "fok_rejected:unknown" branch
  - leg_timeout_s exceeded → TimeoutError handler produces synthetic fills
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from core.types import FillResult
from loop.executor import (
    EntryOutcome,
    _combine_reasons,
    parallel_market_fok_entry,
)


def _fill(symbol: str, leg: str, accepted: bool, error: str | None = None) -> FillResult:
    return FillResult(
        symbol=symbol,
        venue_leg=leg,
        side="buy" if leg == "spot" else "sell",
        qty=0.0 if not accepted else 1.0,
        avg_price=0.0,
        fee_quote=0.0,
        accepted=accepted,
        error=error,
    )


def _gateway_with_fills(spot_fill: FillResult, perp_fill: FillResult) -> MagicMock:
    gw = MagicMock()

    def _place(symbol, venue_leg, side, qty, coid):
        return spot_fill if venue_leg == "spot" else perp_fill

    gw.place_market_fok.side_effect = _place
    return gw


# ─── both-legs-rejected → REJECTED outcome ────────────────────────────────────


def test_both_legs_rejected_returns_rejected_outcome():
    """Both fills not accepted → EntryOutcome.REJECTED with combined reason string."""
    spot = _fill("X/USDT", "spot", accepted=False, error="price_out_of_range")
    perp = _fill("X/USDT:USDT", "futures", accepted=False, error="insufficient_margin")
    gw = _gateway_with_fills(spot, perp)

    result = parallel_market_fok_entry(
        gw,
        spot_symbol="X/USDT",
        perp_symbol="X/USDT:USDT",
        qty=1.0,
        spot_coid="s-1",
        perp_coid="p-1",
    )

    assert result.outcome == EntryOutcome.REJECTED
    assert "spot:price_out_of_range" in result.reason
    assert "perp:insufficient_margin" in result.reason
    assert result.spot_fill is not None and not result.spot_fill.accepted
    assert result.perp_fill is not None and not result.perp_fill.accepted


# ─── _combine_reasons ─────────────────────────────────────────────────────────


def test_combine_reasons_no_errors_returns_unknown():
    """Fills with error=None → fallback 'fok_rejected:unknown'."""
    spot = _fill("X/USDT", "spot", accepted=False, error=None)
    perp = _fill("X/USDT:USDT", "futures", accepted=False, error=None)
    assert _combine_reasons(spot, perp) == "fok_rejected:unknown"


def test_combine_reasons_none_fills_returns_unknown():
    """None fills → fallback 'fok_rejected:unknown'."""
    assert _combine_reasons(None, None) == "fok_rejected:unknown"


# ─── leg timeout → synthetic fills ───────────────────────────────────────────


def test_leg_timeout_produces_synthetic_fills():
    """Gateway calls exceed leg_timeout_s → TimeoutError handler runs,
    producing synthetic FillResult(error='leg_timeout') for both legs."""
    gw = MagicMock()

    def _slow(symbol, venue_leg, side, qty, coid):
        time.sleep(0.3)
        return _fill(symbol, venue_leg, accepted=True)

    gw.place_market_fok.side_effect = _slow

    result = parallel_market_fok_entry(
        gw,
        spot_symbol="X/USDT",
        perp_symbol="X/USDT:USDT",
        qty=1.0,
        spot_coid="s-1",
        perp_coid="p-1",
        leg_timeout_s=0.01,
    )

    assert result.outcome == EntryOutcome.REJECTED
    assert result.spot_fill is not None
    assert result.spot_fill.error == "leg_timeout"
    assert result.perp_fill is not None
    assert result.perp_fill.error == "leg_timeout"
