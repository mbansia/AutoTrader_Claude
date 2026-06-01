"""Gate math + APY + basis tests. Mirrors §3.1 math + §0.6 worked example."""

from __future__ import annotations


from core.math import (
    apy_compound,
    basis_bps,
    basis_dislocation_triggered,
    basis_rt_signed_bps,
    evaluate_gate,
    fees_rt_bps,
    forward_net_apy_for_exit,
    periods_per_year,
    unrealized_pnl_fraction,
    worst_adverse_swing_bps,
)
from core.types import GateInputs


def test_periods_per_year_8h():
    assert periods_per_year(8.0) == 24.0 * 365.0 / 8.0
    assert periods_per_year(4.0) == 24.0 * 365.0 / 4.0


def test_apy_worked_example_8h():
    """§0.6: 0.01% per 8h → APY ≈ 11.57%."""
    apy = apy_compound(per_window_bps=1.0, funding_interval_hours=8.0)
    assert abs(apy - 0.1157) < 1e-3


def test_apy_worked_example_4h():
    """§0.6: 0.01% per 4h → APY ≈ 24.49%."""
    apy = apy_compound(per_window_bps=1.0, funding_interval_hours=4.0)
    assert abs(apy - 0.2449) < 1e-3


def test_basis_sign_convention():
    """§0.1: positive basis = perp premium to spot."""
    assert basis_bps(50_050, 50_000) > 0
    assert basis_bps(50_000, 50_050) < 0


def test_worst_adverse_swing_l05():
    """§16 L05: worst-case is `m × |b_e|` regardless of sign."""
    assert worst_adverse_swing_bps(27.0, 3.0) == 81.0
    assert worst_adverse_swing_bps(-27.0, 3.0) == 81.0


def test_basis_rt_signed_l05():
    """Always negative regardless of entry-basis sign."""
    assert basis_rt_signed_bps(27.0, 3.0) == -81.0
    assert basis_rt_signed_bps(-27.0, 3.0) == -81.0


def test_fees_rt_no_swap():
    assert fees_rt_bps(6.0, 6.0) == 24.0


def test_fees_rt_with_swap():
    """Auto-swap adds 2 × (spot_fee + swap_basis)."""
    assert fees_rt_bps(6.0, 6.0, swap_basis_bps=5.0) == 24.0 + 2 * (6.0 + 5.0)


def test_evaluate_gate_low_funding_rejects():
    g = GateInputs(
        funding_rate=0.00025,            # 2.5 bps per window
        funding_interval_hours=4.0,
        entry_basis_bps=30.0,
        spot_fee_bps=6.0,
        perp_fee_bps=6.0,
        exit_basis_buffer_multiple=3.0,
    )
    r = evaluate_gate(g)
    # 2.5 - 90 - 24 = -111.5 bps per window → deeply negative APY.
    assert r.net_per_window_bps == -111.5
    assert r.net_apy < 0


def test_evaluate_gate_high_funding_passes():
    """A pair with 0.4% funding / 4h, 0 basis, 6 bps fees: per-window net
    40 - 0 - 24 = 16 bps → (1 + 0.0016)^2190 - 1 ≈ huge.
    """
    g = GateInputs(
        funding_rate=0.004,
        funding_interval_hours=4.0,
        entry_basis_bps=0.0,
        spot_fee_bps=6.0,
        perp_fee_bps=6.0,
        exit_basis_buffer_multiple=3.0,
    )
    r = evaluate_gate(g)
    assert r.net_per_window_bps == 16.0
    assert r.net_apy > 30.0  # arbitrarily large


def test_basis_dislocation_trigger():
    """v1.3: `b_l - b_e > threshold` mandatory exit."""
    assert basis_dislocation_triggered(10.0, 60.0, 49.0)
    assert basis_dislocation_triggered(10.0, 61.0, 50.0)
    assert not basis_dislocation_triggered(10.0, 59.0, 50.0)
    # Symmetric for negative entry basis:
    assert basis_dislocation_triggered(-10.0, 41.0, 50.0)
    assert not basis_dislocation_triggered(-10.0, 39.0, 50.0)


def test_forward_net_apy_for_exit_matches_entry_form():
    """Exit gate is the entry gate with b_l replacing b_e."""
    fwd = forward_net_apy_for_exit(
        live_funding_rate=0.001,
        funding_interval_hours=8.0,
        live_basis_bps_value=15.0,
        spot_fee_bps=5.0,
        perp_fee_bps=5.0,
        exit_basis_buffer_multiple=3.0,
    )
    entry = evaluate_gate(
        GateInputs(
            funding_rate=0.001,
            funding_interval_hours=8.0,
            entry_basis_bps=15.0,
            spot_fee_bps=5.0,
            perp_fee_bps=5.0,
            exit_basis_buffer_multiple=3.0,
        )
    ).net_apy
    assert abs(fwd - entry) < 1e-9


def test_unrealized_pnl_neutral_at_entry():
    """Long spot + short perp: PnL = qty × (spot_mark - spot_entry)
    + qty × (perp_entry - perp_mark). At marks=entries → zero.
    """
    pnl = unrealized_pnl_fraction(0.1, 50_000, 50_050, 50_000, 50_050)
    assert pnl == 0.0


def test_unrealized_pnl_basis_widening():
    """Basis widens against us: spot mark unchanged, perp mark higher than
    entry → negative PnL on the short perp leg."""
    # entry: spot 50k, perp 50,050 (10 bps basis); mark: spot 50k, perp 50,100
    pnl = unrealized_pnl_fraction(0.1, 50_000, 50_050, 50_000, 50_100)
    # spot leg: 0.1 × 0 = 0; perp leg: 0.1 × (50050 - 50100) = -5
    # entry notional: 0.1 × 50000 = 5000; fraction = -5/5000 = -0.001
    assert abs(pnl - -0.001) < 1e-9


def test_unrealized_pnl_market_moves_neutral():
    """Both legs equal qty: a parallel price move leaves PnL invariant."""
    pnl = unrealized_pnl_fraction(0.1, 50_000, 50_050, 60_000, 60_050)
    # spot: 0.1 × 10000 = 1000; perp: 0.1 × (50050 - 60050) = -1000 → net 0
    assert abs(pnl) < 1e-9
