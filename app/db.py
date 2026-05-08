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
    # Position sizing as % of portfolio.
    _add_column_if_missing('strategy_config', 'min_position_pct', 'FLOAT NOT NULL DEFAULT 0.005')
    _add_column_if_missing('strategy_config', 'max_position_pct', 'FLOAT NOT NULL DEFAULT 0.10')
    # Per-position funding-income tracking.
    _add_column_if_missing('positions', 'funding_income_accrued', 'FLOAT NOT NULL DEFAULT 0.0')
    _add_column_if_missing('positions', 'last_funding_accrual_ts', "DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00'")
    # Auto-transfer USDT between spot and futures wallets.
    _add_column_if_missing('strategy_config', 'auto_transfer_enabled', 'BOOLEAN NOT NULL DEFAULT 1')
    _add_column_if_missing('strategy_config', 'auto_rebalance_threshold', 'FLOAT NOT NULL DEFAULT 1.0')
    _add_column_if_missing('strategy_config', 'futures_buffer_pct', 'FLOAT NOT NULL DEFAULT 0.20')
    _add_column_if_missing('strategy_config', 'max_perp_leverage', 'INTEGER NOT NULL DEFAULT 1')
    # Trade-type taxonomy — every position / trade carries one tag so the
    # dashboard can group by strategy and the orchestrator can apply the
    # right buffer / sizing rules per type. Same-venue funding-arb is the
    # only active type today; defaults assume the legacy Binance gateway.
    _add_column_if_missing('positions', 'trade_type', "VARCHAR(48) NOT NULL DEFAULT 'binance_same_venue_funding_arb'")
    _add_column_if_missing('trades', 'trade_type', "VARCHAR(48) NOT NULL DEFAULT 'binance_same_venue_funding_arb'")
    # One-shot back-fill: existing rows tagged as 'binance_funding_arb'
    # by default; rewrite KuCoin rows to the kucoin tag.
    insp_tt = inspect(engine)
    if 'positions' in insp_tt.get_table_names():
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE positions SET trade_type = 'kucoin_same_venue_funding_arb' "
                "WHERE exchange = 'kucoin' AND trade_type = 'binance_same_venue_funding_arb'"
            ))
            conn.execute(text(
                "UPDATE trades SET trade_type = 'kucoin_same_venue_funding_arb' "
                "WHERE exchange = 'kucoin' AND trade_type = 'binance_same_venue_funding_arb'"
            ))
    _add_column_if_missing('strategy_config', 'perp_leverage', 'INTEGER NOT NULL DEFAULT 1')
    _add_column_if_missing('strategy_config', 'min_order_book_depth_usdt', 'FLOAT NOT NULL DEFAULT 500.0')
    _add_column_if_missing('strategy_config', 'depth_band_bps', 'FLOAT NOT NULL DEFAULT 10.0')
    _add_column_if_missing('positions', 'last_close_error', "TEXT NOT NULL DEFAULT ''")
    # Per-position quote currency — 'USDT' or 'USDC'. Default USDT on
    # every existing row preserves the historical assumption.
    _add_column_if_missing('positions', 'quote_currency', "VARCHAR(8) NOT NULL DEFAULT 'USDT'")
    _add_column_if_missing('positions', 'spot_quote_currency', "VARCHAR(8) NOT NULL DEFAULT 'USDT'")
    # Limit-IOC tick buffers for protected execution.
    _add_column_if_missing('strategy_config', 'entry_tick_buffer_bps', 'FLOAT NOT NULL DEFAULT 1.0')
    _add_column_if_missing('strategy_config', 'exit_tick_buffer_bps', 'FLOAT NOT NULL DEFAULT 2.0')
    # Profitability gate: replaces the hard-coded basis filter with
    # an expected-net-profit-per-window check using actual fill
    # prices, taker fees, and a worst-case exit-basis buffer.
    _add_column_if_missing('strategy_config', 'taker_fee_bps', 'FLOAT NOT NULL DEFAULT 5.0')
    _add_column_if_missing('strategy_config', 'exit_basis_buffer_multiple', 'FLOAT NOT NULL DEFAULT 3.0')
    _add_column_if_missing('strategy_config', 'min_window_profit_bps', 'FLOAT NOT NULL DEFAULT 0.0')
    _add_column_if_missing('strategy_config', 'auto_quote_swap_enabled', 'BOOLEAN NOT NULL DEFAULT 1')
    # Cross-venue tag — every per-row table carries an ``exchange`` column so
    # the dashboard, logs, scans, and exports can break down state by venue
    # without ambiguity. Default 'binance' on every existing row guarantees
    # the existing data renders correctly the first time the migration runs.
    for table in ('positions', 'trades', 'balance_snapshots', 'equity_curve',
                  'rejected_candidates', 'bot_events', 'capital_flows', 'scan_results'):
        _add_column_if_missing(table, 'exchange', "VARCHAR(16) NOT NULL DEFAULT 'binance'")
    # Stable per-venue id for auto-ingested capital flows (so re-runs don't
    # duplicate the same deposit / withdrawal / sub-transfer row).
    _add_column_if_missing('capital_flows', 'external_id', "VARCHAR(128) NOT NULL DEFAULT ''")
    # The legacy ``earn_state`` table is left in place if it exists in old
    # DBs (the yield-optimisation subsystem was removed; see archive/yield/).
    # No active code reads it.

    # One-shot cleanup: the *old* auto-detect heuristic created CapitalFlow
    # rows whenever the live wallet drifted from the previous snapshot —
    # which mistook funding payments and mark-price moves for phantom
    # "withdrawals". Those rows were written without an external_id (the
    # column didn't exist yet). Purge only those legacy rows; modern auto
    # rows ingested from venue history carry external_id and are kept so
    # XIRR survives restarts.
    insp = inspect(engine)
    if 'capital_flows' in insp.get_table_names():
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM capital_flows WHERE detected_by = 'auto' AND (external_id IS NULL OR external_id = '')"))
            # The manual capital-injection form was retired in favor of
            # API-derived flows (the user wanted nothing to be assumed —
            # only live data from the venue). Wipe any leftover manual rows
            # so net-injected-capital and XIRR reflect API truth only.
            conn.execute(text("DELETE FROM capital_flows WHERE detected_by = 'manual'"))
            # One-shot cleanup: earlier ingest of Binance universal-transfer
            # history kept *intra-account* moves (spot↔futures, margin↔spot,
            # etc.) — those are the bot's own pre-PM shuttle, not capital
            # flowing in/out of the account. The notes carry the "Universal
            # transfer X→Y" label; purge any auto rows whose note matches an
            # intra-account direction so Net Injected Capital is clean.
            for direction in (
                'spot→linear', 'linear→spot', 'spot→inverse', 'inverse→spot',
                'spot→margin', 'margin→spot', 'spot→funding', 'funding→spot',
                'main→linear', 'linear→main', 'main→margin', 'margin→main',
                'main→funding', 'funding→main', 'main→spot', 'spot→main',
                'spot→swap', 'swap→spot', 'umfuture→spot', 'spot→umfuture',
                'cmfuture→spot', 'spot→cmfuture',
            ):
                conn.execute(text(f"DELETE FROM capital_flows WHERE detected_by = 'auto' AND note LIKE '%{direction}%'"))
