"""Safety guards — lightweight, pure functions and exchange-state queries.

The strategy is delta-neutral by construction; this module enforces that
property and provides the basis-aware gating for entries and voluntary
exits.

* :func:`basis_bps` — ``(perp − spot) / spot`` in basis points. Positive
  means perp trades above spot.
* :func:`is_basis_entry_acceptable` — refuse to open when the spread is
  too wide in either direction. Captures the dislocation risk implied by
  a large basis at entry.
* :func:`is_basis_exit_favorable` — for a long-spot/short-perp arb, the
  close direction is "sell spot, buy perp", which profits when basis ≤ 0.
  We allow up to ``max_bps`` of unfavorable basis before deferring exit.
* :func:`check_hedge` — verifies both legs are still on Binance with
  approximately the expected size; returns the surviving leg name when
  one side has gone naked.
* :func:`check_market_health` — flags a position when either leg's
  market on Binance is no longer in TRADING status (delisting, halt,
  pre-settlement etc).
"""

from __future__ import annotations

from app.exchange import BinanceGateway
from app.models import Position


HEDGE_TOLERANCE = 0.05  # 5% size tolerance before declaring a leg "missing"


def basis_bps(spot_price: float, perp_price: float) -> float:
    """Return (perp - spot) / spot in basis points. Positive = perp trading above spot."""
    if not spot_price or spot_price <= 0:
        return 0.0
    return (perp_price - spot_price) / spot_price * 10_000.0


def is_basis_entry_acceptable(spot_price: float, perp_price: float, max_abs_bps: float) -> tuple[bool, float]:
    bps = basis_bps(spot_price, perp_price)
    return abs(bps) <= max_abs_bps, bps


def is_basis_exit_favorable(spot_price: float, perp_price: float, max_bps: float) -> tuple[bool, float]:
    """For a long-spot/short-perp position, closing means: sell spot, buy perp.
    Profit on the spread when perp ≤ spot at close (basis ≤ 0 ideal). Allow up to max_bps slack."""
    bps = basis_bps(spot_price, perp_price)
    return bps <= max_bps, bps


def check_hedge(gateway: BinanceGateway, position: Position) -> tuple[bool, str, str | None]:
    """Verify both legs exist on the exchange with the expected size.

    Returns (is_hedged, reason, surviving_leg) where surviving_leg ∈ {'spot', 'perp', None}.
    surviving_leg is the leg that *is* still present when the other has gone naked, so the
    caller knows which side to close to flatten the exposure.
    """
    raw = gateway.open_perp_positions_raw()
    perp_amt = 0.0
    for r in raw:
        if r.get('symbol') == position.perp_symbol:
            perp_amt = abs(float(r.get('contracts') or r.get('info', {}).get('positionAmt') or 0))
            break
    expected = position.quantity
    perp_present = perp_amt >= expected * (1 - HEDGE_TOLERANCE)

    bals = gateway.safe_balances()
    if bals is None:
        return True, 'spot_unverified', None  # cannot verify spot — refuse to declare naked
    base = position.spot_symbol.split('/')[0]
    spot_balance = bals.get('spot', {}).get(base) or {}
    spot_total = float(spot_balance.get('total') or 0)
    spot_present = spot_total >= expected * (1 - HEDGE_TOLERANCE)

    if perp_present and spot_present:
        return True, 'ok', None
    if perp_present and not spot_present:
        return False, f'spot leg missing (have {spot_total:.6f}, expected ~{expected:.6f})', 'perp'
    if spot_present and not perp_present:
        return False, f'perp leg missing (have {perp_amt:.6f}, expected ~{expected:.6f})', 'spot'
    return False, 'both legs missing', None


def check_market_health(gateway: BinanceGateway, position: Position) -> tuple[bool, str]:
    """Detect delisting / paused / inactive markets. Returns (healthy, reason)."""
    spot_market = (gateway.spot.markets or {}).get(position.spot_symbol) or {}
    if spot_market and spot_market.get('active') is False:
        return False, 'spot market inactive'
    spot_status = (spot_market.get('info') or {}).get('status')
    if spot_status and spot_status != 'TRADING':
        return False, f'spot status={spot_status}'

    perp_market = (gateway.futures.markets or {}).get(position.perp_symbol) or {}
    if perp_market and perp_market.get('active') is False:
        return False, 'perp market inactive'
    perp_info = perp_market.get('info') or {}
    perp_status = perp_info.get('contractStatus') or perp_info.get('status')
    if perp_status and perp_status != 'TRADING':
        return False, f'perp status={perp_status}'
    return True, 'ok'
