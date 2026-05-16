"""Engine, session factory, additive migrations. §7.3 policy."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
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
    return create_engine(final_url, future=True)


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
    """
    eng = engine or get_engine()
    Base.metadata.create_all(eng)
    # v1.4 additive columns (sub_target_sizing_factor, depeg_guard_bps)
    _add_column_if_missing(
        eng,
        "strategy_config_per_strategy",
        "sub_target_sizing_factor",
        "FLOAT NOT NULL DEFAULT 0.75",
    )
    _add_column_if_missing(
        eng,
        "strategy_config_per_strategy",
        "depeg_guard_bps",
        "FLOAT NOT NULL DEFAULT 25.0",
    )
    # v1.4 anomaly thresholds on global config
    _add_column_if_missing(
        eng,
        "strategy_config",
        "cycle_error_rate_threshold",
        "FLOAT NOT NULL DEFAULT 0.10",
    )
    _add_column_if_missing(
        eng,
        "strategy_config",
        "slippage_alert_bps",
        "FLOAT NOT NULL DEFAULT 10.0",
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
