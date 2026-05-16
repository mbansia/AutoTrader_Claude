"""DB layer. Schema matches docs/SYSTEM.md §7.1.1; migrations are additive only."""

from .db import build_engine, get_engine, get_session_factory, init_db, run_migrations, session_scope
from .repository import (
    OPEN_STATUSES_SQL,
    active_exchange_ids,
    event_counts_in_window,
    get_or_create_mode_state,
    get_or_create_strategy_config,
    get_or_create_strategy_state,
    get_or_seed_per_strategy_config,
    get_venue_state,
    insert_position,
    insert_rejection,
    insert_trade,
    list_venue_states,
    log_event,
    positions_by_status,
    positions_currently_exposed,
    recent_events,
    recent_trades,
    rejections_grouped,
)
