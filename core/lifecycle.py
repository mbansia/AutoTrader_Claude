"""Position state-machine. Pure function from (current_status, transition_event)
to next_status, with explicit guards. Implements §0.4 + §7.2 lifecycle.

Statuses: open ↔ naked_spot ↔ closed; open ↔ naked_perp ↔ closed.
Both naked statuses count as "currently exposed".
"""

from __future__ import annotations

from enum import Enum

from .types import PositionStatus


class LifecycleEvent(str, Enum):
    OPEN_BOTH_LEGS_FILLED = "open_both_legs_filled"
    OPEN_ONLY_SPOT_FILLED = "open_only_spot_filled"   # entry: perp leg rejected/zero-fill
    OPEN_ONLY_PERP_FILLED = "open_only_perp_filled"   # exit: spot sold but perp buy-back failed
    DISCOVER_ORPHAN_SPOT = "discover_orphan_spot"     # spot in wallet but no DB row
    DISCOVER_ORPHAN_PERP = "discover_orphan_perp"     # perp short on venue but no DB row
    HEDGE_RECOVERED = "hedge_recovered"               # naked → open via recovery hedge
    FLAT_CLOSED = "flat_closed"                       # both legs zero, normal close path
    STALE_RECONCILED = "stale_reconciled"             # naked row's underlying leg gone
    DUST_CONVERTED = "dust_converted"                 # naked_spot below MIN_NOTIONAL swept


class IllegalTransition(Exception):
    pass


_TRANSITIONS: dict[tuple[PositionStatus, LifecycleEvent], PositionStatus] = {
    (PositionStatus.OPEN, LifecycleEvent.FLAT_CLOSED): PositionStatus.CLOSED,
    (PositionStatus.OPEN, LifecycleEvent.OPEN_ONLY_SPOT_FILLED): PositionStatus.NAKED_SPOT,
    (PositionStatus.OPEN, LifecycleEvent.OPEN_ONLY_PERP_FILLED): PositionStatus.NAKED_PERP,
    # Mid-life hedge-integrity discovery: a live open row finds one leg
    # vanished on the venue. The surviving leg is whichever didn't disappear.
    (PositionStatus.OPEN, LifecycleEvent.DISCOVER_ORPHAN_SPOT): PositionStatus.NAKED_SPOT,
    (PositionStatus.OPEN, LifecycleEvent.DISCOVER_ORPHAN_PERP): PositionStatus.NAKED_PERP,
    (PositionStatus.NAKED_SPOT, LifecycleEvent.HEDGE_RECOVERED): PositionStatus.OPEN,
    (PositionStatus.NAKED_PERP, LifecycleEvent.HEDGE_RECOVERED): PositionStatus.OPEN,
    (PositionStatus.NAKED_SPOT, LifecycleEvent.FLAT_CLOSED): PositionStatus.CLOSED,
    (PositionStatus.NAKED_PERP, LifecycleEvent.FLAT_CLOSED): PositionStatus.CLOSED,
    (PositionStatus.NAKED_SPOT, LifecycleEvent.STALE_RECONCILED): PositionStatus.CLOSED,
    (PositionStatus.NAKED_PERP, LifecycleEvent.STALE_RECONCILED): PositionStatus.CLOSED,
    (PositionStatus.NAKED_SPOT, LifecycleEvent.DUST_CONVERTED): PositionStatus.CLOSED,
}


def initial_status(event: LifecycleEvent) -> PositionStatus:
    """The status to persist for a newly-created Position row."""
    if event == LifecycleEvent.OPEN_BOTH_LEGS_FILLED:
        return PositionStatus.OPEN
    if event in (LifecycleEvent.OPEN_ONLY_SPOT_FILLED, LifecycleEvent.DISCOVER_ORPHAN_SPOT):
        return PositionStatus.NAKED_SPOT
    if event in (LifecycleEvent.OPEN_ONLY_PERP_FILLED, LifecycleEvent.DISCOVER_ORPHAN_PERP):
        return PositionStatus.NAKED_PERP
    raise IllegalTransition(f"No initial status for event {event.value}")


def transition(current: PositionStatus, event: LifecycleEvent) -> PositionStatus:
    """Mapping current → next. Raises IllegalTransition if no rule covers it."""
    if current == PositionStatus.CLOSED:
        raise IllegalTransition(f"Cannot transition from CLOSED on {event.value}")
    key = (current, event)
    if key not in _TRANSITIONS:
        raise IllegalTransition(f"No rule for ({current.value}, {event.value})")
    return _TRANSITIONS[key]


def is_currently_exposed(status: PositionStatus) -> bool:
    return status in (
        PositionStatus.OPEN,
        PositionStatus.NAKED_SPOT,
        PositionStatus.NAKED_PERP,
    )
