"""Database engine, session factory, and lightweight schema migrations.

The bot uses a single SQLite file (configurable via the ``DATABASE_URL``
env var). On startup the parent directory is auto-created so a fresh
Coolify volume mount works without manual setup.

:func:`run_schema_migrations` runs at app startup and applies any
``ALTER TABLE ADD COLUMN`` statements needed to bring an older DB up to
the current model. We keep migrations lightweight rather than pulling in
Alembic — every migration is idempotent and only adds columns that are
backwards-compatible (defaults supplied).
"""

import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings


def _ensure_sqlite_dir(url: str) -> None:
    """Make sure the parent directory exists when DATABASE_URL points at a SQLite file."""
    if not url.startswith('sqlite:'):
        return
    path = url.split('sqlite:///', 1)[-1] if url.startswith('sqlite:///') else ''
    if not path or path == ':memory:':
        return
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


_ensure_sqlite_dir(settings.database_url)

Base = declarative_base()
engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _add_column_if_missing(table: str, column: str, ddl: str) -> None:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return  # create_all will produce it with the right schema
    cols = {c['name'] for c in insp.get_columns(table)}
    if column in cols:
        return
    with engine.begin() as conn:
        conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))


def run_schema_migrations() -> None:
    """Apply lightweight in-place migrations for columns added after the initial schema."""
    _add_column_if_missing('positions', 'funding_interval_hours', 'FLOAT NOT NULL DEFAULT 8.0')
    _add_column_if_missing('strategy_config', 'max_entry_basis_bps', 'FLOAT NOT NULL DEFAULT 20.0')
    _add_column_if_missing('strategy_config', 'max_exit_basis_bps', 'FLOAT NOT NULL DEFAULT 5.0')
    _add_column_if_missing('strategy_config', 'enforce_hedge_check', 'BOOLEAN NOT NULL DEFAULT 1')
    _add_column_if_missing('strategy_config', 'delisting_check', 'BOOLEAN NOT NULL DEFAULT 1')
    # Mode tagging — added when paper/live data was segregated.
    for table in ('positions', 'trades', 'equity_curve', 'rejected_candidates', 'bot_events', 'capital_flows', 'scan_results'):
        _add_column_if_missing(table, 'mode', "VARCHAR(8) NOT NULL DEFAULT 'paper'")
    # Position sizing as % of portfolio + Earn sweep.
    _add_column_if_missing('strategy_config', 'min_position_pct', 'FLOAT NOT NULL DEFAULT 0.005')
    _add_column_if_missing('strategy_config', 'max_position_pct', 'FLOAT NOT NULL DEFAULT 0.10')
    _add_column_if_missing('strategy_config', 'earn_enabled', 'BOOLEAN NOT NULL DEFAULT 0')
    _add_column_if_missing('strategy_config', 'earn_idle_threshold_usdt', 'FLOAT NOT NULL DEFAULT 1.0')
    _add_column_if_missing('strategy_config', 'earn_paper_apr', 'FLOAT NOT NULL DEFAULT 0.05')
    # Per-position funding-income tracking.
    _add_column_if_missing('positions', 'funding_income_accrued', 'FLOAT NOT NULL DEFAULT 0.0')
    _add_column_if_missing('positions', 'last_funding_accrual_ts', "DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00'")
    # Auto-transfer USDT between spot and futures wallets.
    _add_column_if_missing('strategy_config', 'auto_transfer_enabled', 'BOOLEAN NOT NULL DEFAULT 1')
    _add_column_if_missing('strategy_config', 'auto_rebalance_threshold', 'FLOAT NOT NULL DEFAULT 1.0')
    _add_column_if_missing('strategy_config', 'earn_subscribe_spot_assets', 'BOOLEAN NOT NULL DEFAULT 0')
    _add_column_if_missing('strategy_config', 'perp_leverage', 'INTEGER NOT NULL DEFAULT 1')
    _add_column_if_missing('strategy_config', 'min_order_book_depth_usdt', 'FLOAT NOT NULL DEFAULT 500.0')
    _add_column_if_missing('strategy_config', 'depth_band_bps', 'FLOAT NOT NULL DEFAULT 10.0')
    _add_column_if_missing('positions', 'last_close_error', "TEXT NOT NULL DEFAULT ''")
    # Cross-venue tag — every per-row table carries an ``exchange`` column so
    # the dashboard, logs, scans, and exports can break down state by venue
    # without ambiguity. Default 'binance' on every existing row guarantees
    # the existing data renders correctly the first time the migration runs.
    for table in ('positions', 'trades', 'balance_snapshots', 'equity_curve',
                  'rejected_candidates', 'bot_events', 'capital_flows', 'scan_results'):
        _add_column_if_missing(table, 'exchange', "VARCHAR(16) NOT NULL DEFAULT 'binance'")

    # One-shot cleanup: the old auto-detect heuristic created CapitalFlow rows
    # with detected_by='auto' whenever the live wallet drifted from the previous
    # snapshot. That was wrong — funding payments and mark-price moves both
    # showed up as phantom "withdrawals". The heuristic was removed but rows it
    # already created sit in the table as fake history. Purge them on startup.
    # Manual rows (detected_by='manual') and Binance-history-derived rows are
    # untouched.
    insp = inspect(engine)
    if 'capital_flows' in insp.get_table_names():
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM capital_flows WHERE detected_by = 'auto'"))
