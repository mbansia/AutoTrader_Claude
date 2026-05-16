"""Background runner tests. Verifies start/stop semantics + activation rules.

Post-v1.6: activation is in the DB (`venue_state`), not env. The deprecated
`ACTIVE_EXCHANGES` env var still mirrors into the DB on first boot for
back-compat.
"""

from __future__ import annotations

import importlib

import pytest


def _set_venue_active(exchange_id: str, *, active: bool, expected_id: str = "") -> None:
    """Helper: directly mutate venue_state for test setup."""
    from state import session_scope
    from state.repository import get_venue_state

    with session_scope() as session:
        row = get_venue_state(session, exchange_id)
        if row is not None:
            row.active = active
            row.expected_account_id = expected_id


@pytest.fixture(autouse=True)
def isolate_registry_and_workers(monkeypatch, tmp_path):
    """Each test gets a fresh DB + empty registry + empty worker list."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'runner.db'}")
    import state.db as db_mod
    importlib.reload(db_mod)
    from core import config as core_config
    core_config.clear_registry()
    import loop.runner as runner_mod
    importlib.reload(runner_mod)
    # Init DB so venue_state defaults are seeded.
    db_mod.init_db()
    yield
    runner_mod.stop_runner(timeout_s=2.0)
    core_config.clear_registry()


def test_default_venue_state_seed():
    """First boot seeds binance + kucoin active=true, hyperliquid active=false."""
    from state import session_scope
    from state.repository import list_venue_states

    with session_scope() as session:
        rows = {v.exchange_id: v.active for v in list_venue_states(session)}
    assert rows == {"binance": True, "kucoin": True, "hyperliquid": False}


def test_runner_opt_out_with_env(monkeypatch):
    monkeypatch.setenv("BOT_WORKER_ENABLED", "0")
    import loop.runner as r
    assert r.start_runner() == []


def test_runner_skips_when_no_credentials(monkeypatch):
    """DB says binance active, but env has no creds → skip with warning."""
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    monkeypatch.delenv("BOT_WORKER_ENABLED", raising=False)
    import loop.runner as r
    assert r.start_runner() == []


def test_runner_hyperliquid_default_off(monkeypatch):
    """Even with HL creds present, HL is OFF in the seeded DB."""
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0x" + "00" * 20)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    import loop.runner as r
    handles = r.start_runner()
    assert all(h.exchange_id != "hyperliquid" for h in handles)


def test_runner_activates_after_db_toggle(monkeypatch):
    """Dashboard toggles binance off → runner doesn't spawn it."""
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    # Turn binance off via the DB before starting.
    _set_venue_active("binance", active=False)
    import loop.runner as r
    handles = r.start_runner()
    assert handles == []


def test_runner_activates_binance_when_db_active_and_creds_present(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    import loop.runner as r
    handles = r.start_runner()
    assert len(handles) == 2  # paper + live
    assert {h.mode for h in handles} == {"paper", "live"}
    assert all(h.exchange_id == "binance" for h in handles)
    r.stop_runner(timeout_s=2.0)
    assert not any(h.thread.is_alive() for h in handles)


def test_runner_is_idempotent(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    import loop.runner as r
    first = r.start_runner()
    second = r.start_runner()
    assert len(first) == len(second)


def test_legacy_active_exchanges_env_mirrors_into_db(monkeypatch):
    """Back-compat: ACTIVE_EXCHANGES env var one-shot writes to DB."""
    monkeypatch.setenv("ACTIVE_EXCHANGES", "hyperliquid")
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0x" + "00" * 20)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    import loop.runner as r
    handles = r.start_runner()
    # HL was off in seed; env mirror flipped binance + kucoin off, HL on.
    assert any(h.exchange_id == "hyperliquid" for h in handles)
    # Verify the mirror persisted in the DB.
    from state import session_scope
    from state.repository import get_venue_state
    with session_scope() as session:
        v = get_venue_state(session, "hyperliquid")
        assert v is not None and v.active is True


def test_account_id_mismatch_aborts_live_worker(monkeypatch):
    """DB expected_account_id mismatch → live worker not spawned."""
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0xAAA")
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    _set_venue_active("hyperliquid", active=True, expected_id="0xBBB")
    import loop.runner as r
    handles = r.start_runner()
    live_hl = [h for h in handles if h.mode == "live" and h.exchange_id == "hyperliquid"]
    assert live_hl == []


def test_account_id_match_starts_live_worker(monkeypatch):
    """DB expected_account_id matches actual → live worker spawns."""
    addr = "0x" + "0a" * 20
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", addr)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    _set_venue_active("hyperliquid", active=True, expected_id=addr)
    import loop.runner as r
    handles = r.start_runner()
    live_hl = [h for h in handles if h.mode == "live" and h.exchange_id == "hyperliquid"]
    assert len(live_hl) == 1


def test_account_id_unset_is_soft_probe(monkeypatch):
    """No expected_account_id (DB + env) → log INFO, spawn live worker."""
    addr = "0x" + "0a" * 20
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", addr)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    monkeypatch.delenv("HYPERLIQUID_EXPECTED_ACCOUNT_ID", raising=False)
    _set_venue_active("hyperliquid", active=True, expected_id="")
    import loop.runner as r
    handles = r.start_runner()
    live_hl = [h for h in handles if h.mode == "live" and h.exchange_id == "hyperliquid"]
    assert len(live_hl) == 1


def test_runner_registers_gateways_for_diagnostics(monkeypatch):
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    import loop.runner as r
    from core.config import list_gateways
    r.start_runner()
    registered = {(m, x) for m, x, _ in list_gateways()}
    assert registered == {("paper", "binance"), ("live", "binance")}
