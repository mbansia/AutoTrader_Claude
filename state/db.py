"""Engine, session factory, additive migrations. §7.3 policy."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///bot.db")


def _ensure_sqlite_dir(url: str) -> None:
    if not url.startswith("sqlite:"):
        return
    if "///" not in url:
        return
    path = url.split("sqlite:///", 1)[-1]
    if not path or path == ":memory:":
        return
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)


def build_engine(url: str | None = None) -> Engine:
    final_url = url or _database_url()
    _ensure_sqlite_dir(final_url)
    engine = create_engine(final_url, future=True)
    if final_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _connection_record):
            cursor = dbapi_conn.cursor()
            # WAL mode: allows concurrent readers while one writer holds the lock,
            # eliminating the "database is locked" crashes when 3 runner threads
            # (one per venue) flush balance snapshots + capital flows simultaneously.
            cursor.execute("PRAGMA journal_mode=WAL")
            # Give other writers up to 5 s to finish before raising OperationalError.
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, future=True
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    s = get_session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _add_column_if_missing(engine: Engine, table: str, column: str, ddl: str) -> None:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns(table)}
    if column in existing:
        return
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def run_migrations(engine: Engine | None = None) -> None:
    """Idempotent additive migrations. New columns added to bring an older DB
    up to the current model; never drop. Schema policy §7.3.

    The auto-sync pass (`_sync_columns_to_models`) walks every model and
    ALTER TABLE ADD COLUMNs any missing column with a type-appropriate
    default. This handles the v1.3 → v1.5 column-name drift (e.g. legacy
    `enforce_hedge_check` vs new `hedge_integrity_check`) without having
    to hand-maintain a migration ledger.

    `_copy_legacy_hedge_column` then mirrors the legacy boolean value
    into the new column so a fresh ALTER doesn't reset the operator's
    setting.
    """
    eng = engine or get_engine()
    Base.metadata.create_all(eng)
    _sync_columns_to_models(eng)
    _copy_legacy_hedge_column(eng)


def _sync_columns_to_models(eng: Engine) -> None:
    """For every model-declared column missing from the existing table,
    issue an `ALTER TABLE ADD COLUMN` with a type-appropriate default.
    Idempotent. SQLite requires a DEFAULT on NOT NULL columns.
    """
    insp = inspect(eng)
    existing_tables = set(insp.get_table_names())
    for table in Base.metadata.tables.values():
        if table.name not in existing_tables:
            continue
        existing_cols = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing_cols or col.primary_key:
                continue
            ddl = _alter_add_column_ddl(table.name, col, eng.dialect)
            if not ddl:
                continue
            with eng.begin() as conn:
                conn.execute(text(ddl))


def _alter_add_column_ddl(table_name: str, col, dialect) -> str | None:
    try:
        col_type = col.type.compile(dialect=dialect)
    except Exception:
        return None
    parts = [f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}"]
    default = _default_literal(col)
    if not col.nullable:
        # SQLite ALTER TABLE ADD COLUMN requires a DEFAULT on NOT NULL.
        if default is None:
            default = _default_literal_for_type(col.type)
        parts.append("NOT NULL")
    if default is not None:
        parts.append(f"DEFAULT {default}")
    return " ".join(parts)


def _default_literal(col) -> str | None:
    """Render the SQLAlchemy column's scalar default as a SQL literal.
    Returns None when no default is declared (caller decides what to do).
    """
    if col.default is None:
        return None
    arg = getattr(col.default, "arg", None)
    if callable(arg):
        return None
    if isinstance(arg, bool):
        return "1" if arg else "0"
    if isinstance(arg, (int, float)):
        return str(arg)
    if isinstance(arg, str):
        escaped = arg.replace("'", "''")
        return f"'{escaped}'"
    return None


def _default_literal_for_type(col_type) -> str:
    """Type-appropriate zero default — used when a NOT NULL column has no
    declared default but SQLite requires one for ALTER TABLE.
    """
    name = col_type.__class__.__name__.lower()
    if "boolean" in name or "integer" in name:
        return "0"
    if "float" in name or "numeric" in name or "real" in name:
        return "0.0"
    if "datetime" in name or "date" in name or "time" in name:
        return "'1970-01-01 00:00:00'"
    return "''"


def _copy_legacy_hedge_column(eng: Engine) -> None:
    """v1.3 used `enforce_hedge_check`; v1.5 renames to `hedge_integrity_check`
    (spec terminology, §3.1 SOP). When both columns exist (i.e. after the
    sync pass added the new one to a legacy DB), mirror the legacy value
    so the operator's toggle is preserved. One-shot: subsequent runs see
    `hedge_integrity_check` populated and copy the same value (no-op).
    """
    insp = inspect(eng)
    if "strategy_config" not in insp.get_table_names():
        return
    cols = {c["name"] for c in insp.get_columns("strategy_config")}
    if "enforce_hedge_check" in cols and "hedge_integrity_check" in cols:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "UPDATE strategy_config SET hedge_integrity_check = enforce_hedge_check"
                )
            )


def init_db(engine: Engine | None = None) -> None:
    """Create tables + apply migrations + seed venue defaults. Idempotent."""
    eng = engine or get_engine()
    Base.metadata.create_all(eng)
    run_migrations(eng)
    _seed_venue_defaults(eng)


def _seed_venue_defaults(engine: Engine) -> None:
    """Seed `venue_state` with default rows on first boot. Binance + KuCoin
    default to active=true (the v1.3 baseline); Hyperliquid defaults to
    active=false (opt-in via the dashboard, per §6.4). Subsequent runs
    don't touch existing rows.
    """
    from .models import VenueState

    defaults = (
        ("binance", True),
        ("kucoin", True),
        ("hyperliquid", False),
    )
    with engine.begin() as conn:
        from sqlalchemy import select
        existing = {row[0] for row in conn.execute(select(VenueState.exchange_id)).all()}
        for exchange_id, active in defaults:
            if exchange_id in existing:
                continue
            conn.execute(
                VenueState.__table__.insert().values(
                    exchange_id=exchange_id,
                    active=active,
                    expected_account_id="",
                    notes="",
                )
            )
