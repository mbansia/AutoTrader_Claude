"""Closed-form sizing per side. Replaces the v1.3 iterative book walk
(see §16 L38). Mirrors docs/SYSTEM.md §3.1 math.

The math: for a side that has a price-dependent wallet constraint (a buy
walks the asks; the deeper the level, the worse the price, the less the
wallet can fund), the feasible quantity per level is
`min(cum_qty_i, max_fund_qty_i)` and the optimum is `argmax_i feasible_i`.

For sides with a flat-cap constraint (sell-spot: base balance; perp:
mark-price margin), the binding qty is simply `min(cum_qty_until_filled, cap)`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import BookSnapshot, SizingResult


@dataclass(slots=True)
class SideBinding:
    qty: float
    avg_price: float
    rejection: str | None = None


def _vwap_up_to(levels: list, qty_target: float) -> float:
    """Volume-weighted average price across book levels up to qty_target.
    `levels` are pre-sorted by aggressiveness; each has .price and .depth.
    Returns the last level price if qty_target exceeds cumulative depth.
    """
    if qty_target <= 0.0:
        return 0.0
    spent_qty = 0.0
    spent_quote = 0.0
    for lvl in levels:
        if spent_qty >= qty_target:
            break
        take = min(lvl.depth, qty_target - spent_qty)
        spent_quote += take * lvl.price
        spent_qty += take
    if spent_qty <= 0.0:
        return 0.0
    return spent_quote / spent_qty


def _floor_to_step(value: float, step: float) -> float:
    if step <= 0.0:
        return value
    return (int(value / step)) * step


def size_buy_spot_price_dependent(
    asks: list,
    wallet_quote_free: float,
    safety: float,
) -> SideBinding:
    """Buy spot: reservation is price-dependent. Walk levels once, compute
    per-level feasibility, pick the argmax.
    """
    if wallet_quote_free <= 0.0 or not asks:
        return SideBinding(qty=0.0, avg_price=0.0, rejection="empty_wallet_or_book")

    budget = wallet_quote_free * safety
    cum_qty = 0.0
    best_qty = 0.0
    best_level_idx = -1
    for i, lvl in enumerate(asks):
        if lvl.price <= 0.0:
            continue
        cum_qty += lvl.depth
        max_fund_at_this_price = budget / lvl.price
        feasible_i = min(cum_qty, max_fund_at_this_price)
        if feasible_i > best_qty:
            best_qty = feasible_i
            best_level_idx = i

    if best_qty <= 0.0 or best_level_idx < 0:
        return SideBinding(qty=0.0, avg_price=0.0, rejection="no_feasible_level")

    avg = _vwap_up_to(asks[: best_level_idx + 1], best_qty)
    return SideBinding(qty=best_qty, avg_price=avg)


def size_sell_spot_flat_cap(
    bids: list,
    base_balance_free: float,
    safety: float,
) -> SideBinding:
    """Sell spot: wallet cap is base balance, price-independent.
    Binding qty = min(book cumulative depth, base_balance × safety).
    """
    if base_balance_free <= 0.0 or not bids:
        return SideBinding(qty=0.0, avg_price=0.0, rejection="empty_wallet_or_book")
    cap = base_balance_free * safety
    cum_qty = sum(lvl.depth for lvl in bids)
    qty = min(cum_qty, cap)
    if qty <= 0.0:
        return SideBinding(qty=0.0, avg_price=0.0, rejection="zero_qty")
    avg = _vwap_up_to(bids, qty)
    return SideBinding(qty=qty, avg_price=avg)


def size_perp_flat_cap(
    levels: list,
    mark_price: float,
    wallet_quote_free: float,
    safety: float,
    leverage: float,
) -> SideBinding:
    """Perp leg (long or short): margin is computed off mark_price, fixed
    at submission. Binding qty = min(book cumulative depth, margin cap).
    `levels` is asks for a perp buy (close short), bids for a perp sell (open short).
    """
    if wallet_quote_free <= 0.0 or mark_price <= 0.0 or not levels:
        return SideBinding(qty=0.0, avg_price=0.0, rejection="empty_wallet_or_book")
    cap = (wallet_quote_free * safety * leverage) / mark_price
    cum_qty = sum(lvl.depth for lvl in levels)
    qty = min(cum_qty, cap)
    if qty <= 0.0:
        return SideBinding(qty=0.0, avg_price=0.0, rejection="zero_qty")
    avg = _vwap_up_to(levels, qty)
    return SideBinding(qty=qty, avg_price=avg)


def size_entry(
    spot_book: BookSnapshot,
    perp_book: BookSnapshot,
    spot_wallet_quote_free: float,
    perp_wallet_quote_free: float,
    leverage: float,
    safety: float,
    sub_target: float,
    operator_cap_qty: float,
    spot_lot_step: float,
    perp_lot_step: float,
) -> SizingResult:
    """Entry: BUY spot + SELL perp short.
    Returns the closed-form target qty for the pair (in base units) plus
    forecast avg fill prices for the gate input.
    """
    if not spot_book.asks or not perp_book.bids:
        return SizingResult(0.0, 0.0, 0.0, 0.0, 0.0, "empty_book")

    spot_binding = size_buy_spot_price_dependent(
        spot_book.asks, spot_wallet_quote_free, safety
    )
    perp_binding = size_perp_flat_cap(
        perp_book.bids,
        perp_book.mid_price,
        perp_wallet_quote_free,
        safety,
        leverage,
    )

    if spot_binding.rejection or perp_binding.rejection:
        return SizingResult(
            target_qty=0.0,
            forecast_spot_avg_price=0.0,
            forecast_perp_avg_price=0.0,
            spot_binding_qty=spot_binding.qty,
            perp_binding_qty=perp_binding.qty,
            rejection_reason=spot_binding.rejection or perp_binding.rejection,
        )

    target_qty_raw = min(spot_binding.qty, perp_binding.qty, operator_cap_qty)
    lot_step = max(spot_lot_step, perp_lot_step)
    target_qty = _floor_to_step(target_qty_raw * sub_target, lot_step)

    if target_qty <= 0.0:
        return SizingResult(
            target_qty=0.0,
            forecast_spot_avg_price=spot_binding.avg_price,
            forecast_perp_avg_price=perp_binding.avg_price,
            spot_binding_qty=spot_binding.qty,
            perp_binding_qty=perp_binding.qty,
            rejection_reason="below_lot_step_after_sub_target",
        )

    return SizingResult(
        target_qty=target_qty,
        forecast_spot_avg_price=spot_binding.avg_price,
        forecast_perp_avg_price=perp_binding.avg_price,
        spot_binding_qty=spot_binding.qty,
        perp_binding_qty=perp_binding.qty,
    )


def snapshots_co_temporal(
    spot_book: BookSnapshot, perp_book: BookSnapshot, max_skew_ms: int = 50
) -> bool:
    """L42: closed-form math is only valid if both snapshots are co-temporal.
    Default tolerance 50ms; callers reject the candidate if False.
    """
    delta_ms = abs((spot_book.taken_at - perp_book.taken_at).total_seconds()) * 1000.0
    return delta_ms <= max_skew_ms
