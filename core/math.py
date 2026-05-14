"""Gate math + APY + basis. Pure functions. Mirrors docs/SYSTEM.md §3.1 math.

All bps inputs/outputs are basis points (1 bp = 1e-4). Net APY is a decimal
(0.20 = 20%). No mutable state; safe to call from any thread.
"""

from __future__ import annotations

from .types import GateInputs, GateResult


def periods_per_year(funding_interval_hours: float) -> float:
    return 24.0 * 365.0 / funding_interval_hours


def apy_compound(per_window_bps: float, funding_interval_hours: float) -> float:
    """(1 + per_window/10⁴)^N − 1 — the only annualization the bot uses."""
    n = periods_per_year(funding_interval_hours)
    return (1.0 + per_window_bps / 10_000.0) ** n - 1.0


def basis_bps(perp_price: float, spot_price: float) -> float:
    """Signed basis in bps. Positive = perp at premium to spot."""
    if spot_price <= 0.0:
        return 0.0
    return (perp_price - spot_price) / spot_price * 10_000.0


def worst_adverse_swing_bps(entry_basis_bps: float, m: float) -> float:
    """`m × |b_e|` — always non-negative. The worst-case adverse exit
    assumption is "basis moves further positive" regardless of entry sign,
    per §16 L05.
    """
    return m * abs(entry_basis_bps)


def basis_rt_signed_bps(entry_basis_bps: float, m: float) -> float:
    """The round-trip basis P&L cost — always negative (a cost) regardless
    of entry sign. Encodes the L05 conservatism rule.
    """
    return -worst_adverse_swing_bps(entry_basis_bps, m)


def fees_rt_bps(
    spot_fee_bps: float,
    perp_fee_bps: float,
    swap_basis_bps: float = 0.0,
) -> float:
    """Round-trip taker fees. 2 spot legs + 2 perp legs.
    If `swap_basis_bps > 0`, an auto-swap leg fires (2 × (spot_fee + swap_basis)).
    """
    base = 2.0 * (spot_fee_bps + perp_fee_bps)
    if swap_basis_bps > 0.0:
        base += 2.0 * (spot_fee_bps + swap_basis_bps)
    return base


def evaluate_gate(g: GateInputs) -> GateResult:
    """Tier-3 profitability gate. Returns the full result so callers can log
    every component the gate compared against.
    """
    f_w = g.funding_rate * 10_000.0
    basis_signed = basis_rt_signed_bps(g.entry_basis_bps, g.exit_basis_buffer_multiple)
    rt_fees = fees_rt_bps(g.spot_fee_bps, g.perp_fee_bps, g.swap_basis_bps)
    net_per_window = f_w + basis_signed - rt_fees
    net_apy = apy_compound(net_per_window, g.funding_interval_hours)
    return GateResult(
        net_apy=net_apy,
        net_per_window_bps=net_per_window,
        fees_rt_bps=rt_fees,
        basis_rt_signed_bps=basis_signed,
        funding_per_window_bps=f_w,
    )


def forward_net_apy_for_exit(
    live_funding_rate: float,
    funding_interval_hours: float,
    live_basis_bps_value: float,
    spot_fee_bps: float,
    perp_fee_bps: float,
    exit_basis_buffer_multiple: float,
) -> float:
    """The voluntary-exit gate input: would we open this trade NOW?"""
    g = GateInputs(
        funding_rate=live_funding_rate,
        funding_interval_hours=funding_interval_hours,
        entry_basis_bps=live_basis_bps_value,
        spot_fee_bps=spot_fee_bps,
        perp_fee_bps=perp_fee_bps,
        swap_basis_bps=0.0,
        exit_basis_buffer_multiple=exit_basis_buffer_multiple,
    )
    return evaluate_gate(g).net_apy


def basis_dislocation_triggered(
    entry_basis_bps_value: float,
    live_basis_bps_value: float,
    threshold_bps: float,
) -> bool:
    """v1.3 mandatory exit: `(b_l − b_e) > threshold` → close."""
    return (live_basis_bps_value - entry_basis_bps_value) > threshold_bps


def unrealized_pnl_fraction(
    quantity: float,
    spot_entry_price: float,
    perp_entry_price: float,
    spot_mark: float,
    perp_mark: float,
) -> float:
    """Mark-to-market PnL as a fraction of entry spot notional.

    Long spot + short perp. Both prices in quote. Per §3.1 stop-loss math.
    Spot leg PnL: qty × (spot_mark − spot_entry)
    Perp leg PnL: qty × (perp_entry − perp_mark)
    Entry notional: qty × spot_entry_price
    """
    if quantity <= 0.0 or spot_entry_price <= 0.0:
        return 0.0
    spot_pnl = quantity * (spot_mark - spot_entry_price)
    perp_pnl = quantity * (perp_entry_price - perp_mark)
    return (spot_pnl + perp_pnl) / (quantity * spot_entry_price)
