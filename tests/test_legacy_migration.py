"""Schema-sync migration tests. Verifies the v1.5 startup migrates a
legacy v1.3 SQLite DB without crashing on missing columns + carries the
legacy `enforce_hedge_check` value into the new `hedge_integrity_check`.
"""

from __future__ import annotations

import importlib

from sqlalchemy import create_engine, inspect, text


def _create_legacy_schema(db_path: str) -> None:
    """A minimal v1.3-shaped DB: `strategy_config` exists with the legacy
    column names, missing every v1.5-only column. Mimics what the prod
    deployment actually looks like after years of legacy migrations.
    """
    eng = create_engine(f"sqlite:///{db_path}", future=True)
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE strategy_config (
                id INTEGER PRIMARY KEY,
                max_open_positions INTEGER NOT NULL DEFAULT 1,
                max_trades_per_day INTEGER NOT NULL DEFAULT 8,
                loop_seconds INTEGER NOT NULL DEFAULT 30,
                paper_starting_equity FLOAT NOT NULL DEFAULT 1000.0,
                paper_slippage_bps FLOAT NOT NULL DEFAULT 5.0,
                paper_fee_bps FLOAT NOT NULL DEFAULT 4.0,
                enforce_hedge_check BOOLEAN NOT NULL DEFAULT 1,
                delisting_check BOOLEAN NOT NULL DEFAULT 1,
                config_schema_version INTEGER NOT NULL DEFAULT 2,
                updated_at DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00'
            )
        """))
        # Operator had toggled it OFF in v1.3 — must survive the migration.
        conn.execute(text("INSERT INTO strategy_config (id, enforce_hedge_check) VALUES (1, 0)"))
    eng.dispose()


def test_migration_against_legacy_schema_does_not_crash(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    _create_legacy_schema(str(db))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    import state.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    insp = inspect(db_mod.get_engine())
    cols = {c["name"] for c in insp.get_columns("strategy_config")}
    # v1.5-only columns must now be present.
    for col in (
        "hedge_integrity_check",
        "cycle_error_rate_threshold",
        "slippage_alert_bps",
    ):
        assert col in cols, f"v1.5 column {col} not added to legacy strategy_config"
    # Legacy column preserved (additive policy).
    assert "enforce_hedge_check" in cols


def test_migration_copies_legacy_hedge_value(tmp_path, monkeypatch):
    """Operator's `enforce_hedge_check=0` from v1.3 must survive into v1.5's
    `hedge_integrity_check` — otherwise the migration resets their safety
    toggle silently.
    """
    db = tmp_path / "legacy.db"
    _create_legacy_schema(str(db))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    import state.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    with db_mod.get_engine().begin() as conn:
        row = conn.execute(
            text("SELECT enforce_hedge_check, hedge_integrity_check FROM strategy_config WHERE id = 1")
        ).first()
    assert row is not None
    assert row[0] == 0  # legacy preserved
    assert row[1] == 0  # mirrored to new


def test_migration_runs_clean_on_fresh_db(tmp_path, monkeypatch):
    """Fresh DB: create_all + sync should produce identical schema to running
    create_all alone. Idempotent."""
    db = tmp_path / "fresh.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    import state.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    db_mod.init_db()  # second call must not crash
    insp = inspect(db_mod.get_engine())
    cols = {c["name"] for c in insp.get_columns("strategy_config")}
    assert "hedge_integrity_check" in cols


def test_kucoin_passphrase_env_aliases(monkeypatch):
    """Both KUCOIN_API_PASSPHRASE (legacy) and KUCOIN_PASSPHRASE (v1.5)
    must map to env.kucoin_passphrase. Legacy wins on conflict (we treat
    the existing deploy as the source of truth)."""
    from core.config import load_env

    # Neither set → empty.
    monkeypatch.delenv("KUCOIN_API_PASSPHRASE", raising=False)
    monkeypatch.delenv("KUCOIN_PASSPHRASE", raising=False)
    assert load_env().kucoin_passphrase == ""

    # Only legacy set.
    monkeypatch.setenv("KUCOIN_API_PASSPHRASE", "legacy-pw")
    assert load_env().kucoin_passphrase == "legacy-pw"

    # Only new short form set.
    monkeypatch.delenv("KUCOIN_API_PASSPHRASE")
    monkeypatch.setenv("KUCOIN_PASSPHRASE", "short-pw")
    assert load_env().kucoin_passphrase == "short-pw"

    # Both set → legacy wins (don't surprise the existing deploy).
    monkeypatch.setenv("KUCOIN_API_PASSPHRASE", "legacy-wins")
    monkeypatch.setenv("KUCOIN_PASSPHRASE", "ignored")
    assert load_env().kucoin_passphrase == "legacy-wins"
