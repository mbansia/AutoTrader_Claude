"""State machine tests. §0.4 + §7.2."""

from __future__ import annotations

import pytest

from core.lifecycle import (
    IllegalTransition,
    LifecycleEvent,
    initial_status,
    is_currently_exposed,
    transition,
)
from core.types import OPEN_STATUSES, PositionStatus


def test_open_to_closed():
    assert transition(PositionStatus.OPEN, LifecycleEvent.FLAT_CLOSED) == PositionStatus.CLOSED


def test_open_to_naked_spot_on_partial():
    """Entry: spot filled but perp leg rejected → naked_spot."""
    assert (
        transition(PositionStatus.OPEN, LifecycleEvent.OPEN_ONLY_SPOT_FILLED)
        == PositionStatus.NAKED_SPOT
    )


def test_open_to_naked_perp_on_partial():
    assert (
        transition(PositionStatus.OPEN, LifecycleEvent.OPEN_ONLY_PERP_FILLED)
        == PositionStatus.NAKED_PERP
    )


def test_naked_spot_hedge_recovery():
    assert (
        transition(PositionStatus.NAKED_SPOT, LifecycleEvent.HEDGE_RECOVERED)
        == PositionStatus.OPEN
    )


def test_naked_perp_hedge_recovery():
    assert (
        transition(PositionStatus.NAKED_PERP, LifecycleEvent.HEDGE_RECOVERED)
        == PositionStatus.OPEN
    )


def test_naked_spot_flat_close():
    assert (
        transition(PositionStatus.NAKED_SPOT, LifecycleEvent.FLAT_CLOSED)
        == PositionStatus.CLOSED
    )


def test_naked_perp_flat_close():
    """Symmetric to naked_spot — both must close cleanly via reverse-FOK."""
    assert (
        transition(PositionStatus.NAKED_PERP, LifecycleEvent.FLAT_CLOSED)
        == PositionStatus.CLOSED
    )


def test_stale_reconciliation():
    assert (
        transition(PositionStatus.NAKED_SPOT, LifecycleEvent.STALE_RECONCILED)
        == PositionStatus.CLOSED
    )
    assert (
        transition(PositionStatus.NAKED_PERP, LifecycleEvent.STALE_RECONCILED)
        == PositionStatus.CLOSED
    )


def test_dust_converted_only_for_naked_spot():
    """§10: perp shorts have no native dust-conversion endpoint."""
    assert (
        transition(PositionStatus.NAKED_SPOT, LifecycleEvent.DUST_CONVERTED)
        == PositionStatus.CLOSED
    )
    with pytest.raises(IllegalTransition):
        transition(PositionStatus.NAKED_PERP, LifecycleEvent.DUST_CONVERTED)


def test_closed_is_terminal():
    with pytest.raises(IllegalTransition):
        transition(PositionStatus.CLOSED, LifecycleEvent.FLAT_CLOSED)


def test_initial_status_spot_only_fill():
    assert initial_status(LifecycleEvent.OPEN_ONLY_SPOT_FILLED) == PositionStatus.NAKED_SPOT


def test_initial_status_perp_only_fill():
    assert initial_status(LifecycleEvent.OPEN_ONLY_PERP_FILLED) == PositionStatus.NAKED_PERP


def test_initial_status_discover_orphan_perp():
    """L16 symmetry — both naked discoveries are first-class."""
    assert initial_status(LifecycleEvent.DISCOVER_ORPHAN_PERP) == PositionStatus.NAKED_PERP


def test_exposed_set_includes_both_naked():
    """§0.4: currently exposed = {open, naked_spot, naked_perp}."""
    assert is_currently_exposed(PositionStatus.OPEN)
    assert is_currently_exposed(PositionStatus.NAKED_SPOT)
    assert is_currently_exposed(PositionStatus.NAKED_PERP)
    assert not is_currently_exposed(PositionStatus.CLOSED)


def test_open_statuses_tuple_complete():
    assert PositionStatus.OPEN in OPEN_STATUSES
    assert PositionStatus.NAKED_SPOT in OPEN_STATUSES
    assert PositionStatus.NAKED_PERP in OPEN_STATUSES
    assert PositionStatus.CLOSED not in OPEN_STATUSES
