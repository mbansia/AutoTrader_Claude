"""Background runner tests. Verifies start/stop semantics + activation rules."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def isolate_registry_and_workers(monkeypatch, tmp_path):
    """Each test gets a fresh DB + empty registry + empty worker list."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'runner.db'}")
    import state.db as db_mod
    importlib.reload(db_mod)
    from core import config as core_config
    core_config.clear_registry()
    # Reload runner so its module-level _workers list resets.
    import loop.runner as runner_mod
    importlib.reload(runner_mod)
    yield
    runner_mod.stop_runner(timeout_s=2.0)
    core_config.clear_registry()


def test_runner_opt_out_with_env(monkeypatch):
    monkeypatch.setenv("BOT_WORKER_ENABLED", "0")
    import loop.runner as r
    assert r.start_runner() == []


def test_runner_skips_when_no_credentials(monkeypatch):
    """No creds → no live gateway constructed → no workers spawned."""
    monkeypatch.setenv("BOT_WORKER_ENABLED", "1")
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    monkeypatch.delenv("ACTIVE_EXCHANGES", raising=False)
    import loop.runner as r
    assert r.start_runner() == []


def test_runner_hyperliquid_is_opt_in(monkeypatch):
    """Even with HL creds present, HL is OFF unless ACTIVE_EXCHANGES says so."""
    monkeypatch.setenv("BOT_WORKER_ENABLED", "1")
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0x" + "00" * 20)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.delenv("BINANCE_API_KEY", raising=False)
    monkeypatch.delenv("KUCOIN_API_KEY", raising=False)
    monkeypatch.delenv("ACTIVE_EXCHANGES", raising=False)
    import loop.runner as r
    handles = r.start_runner()
    # default activation set excludes HL even though creds are present
    assert all(h.exchange_id != "hyperliquid" for h in handles)


def test_runner_activates_explicit_exchanges(monkeypatch):
    """ACTIVE_EXCHANGES=binance starts binance paper + live (live uses
    a ccxt client constructed with dummy creds — safe, no network call
    happens until the first cycle, and the cycle catches errors)."""
    monkeypatch.setenv("BOT_WORKER_ENABLED", "1")
    monkeypatch.setenv("ACTIVE_EXCHANGES", "binance")
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    import loop.runner as r
    handles = r.start_runner()
    assert len(handles) == 2  # paper + live
    assert {h.mode for h in handles} == {"paper", "live"}
    assert all(h.exchange_id == "binance" for h in handles)
    r.stop_runner(timeout_s=2.0)
    assert not any(h.thread.is_alive() for h in handles)


def test_runner_is_idempotent(monkeypatch):
    monkeypatch.setenv("BOT_WORKER_ENABLED", "1")
    monkeypatch.setenv("ACTIVE_EXCHANGES", "binance")
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    import loop.runner as r
    first = r.start_runner()
    second = r.start_runner()
    assert len(first) == len(second)
    assert {(h.mode, h.exchange_id) for h in first} == {(h.mode, h.exchange_id) for h in second}


def test_account_id_mismatch_aborts_live_worker(monkeypatch):
    """Boot-time guard: if HYPERLIQUID_EXPECTED_ACCOUNT_ID doesn't match
    the wallet address the gateway reports, the live worker must NOT spawn.
    Operator typo in the env var should fail loud, not start silently.
    """
    monkeypatch.setenv("BOT_WORKER_ENABLED", "1")
    monkeypatch.setenv("ACTIVE_EXCHANGES", "hyperliquid")
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", "0xAAA")
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("HYPERLIQUID_EXPECTED_ACCOUNT_ID", "0xBBB")  # mismatch
    import loop.runner as r
    handles = r.start_runner()
    # Live worker must NOT have been spawned for hyperliquid.
    live_hl = [h for h in handles if h.mode == "live" and h.exchange_id == "hyperliquid"]
    assert live_hl == [], "live HL worker spawned despite account-id mismatch"


def test_account_id_match_starts_live_worker(monkeypatch):
    """When expected == actual, the live worker spawns normally.
    Hyperliquid: the wallet address IS the account id (gateway returns
    `self._wallet_address` from actual_account_id), so expected = wallet
    address = match."""
    monkeypatch.setenv("BOT_WORKER_ENABLED", "1")
    monkeypatch.setenv("ACTIVE_EXCHANGES", "hyperliquid")
    addr = "0x" + "0a" * 20
    monkeypatch.setenv("HYPERLIQUID_WALLET_ADDRESS", addr)
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0x" + "11" * 32)
    monkeypatch.setenv("HYPERLIQUID_EXPECTED_ACCOUNT_ID", addr)
    import loop.runner as r
    handles = r.start_runner()
    live_hl = [h for h in handles if h.mode == "live" and h.exchange_id == "hyperliquid"]
    assert len(live_hl) == 1


def test_runner_registers_gateways_for_diagnostics(monkeypatch):
    """After start, the registry should contain the spawned gateways so
    the /api/diagnostics wallets payload picks them up."""
    monkeypatch.setenv("BOT_WORKER_ENABLED", "1")
    monkeypatch.setenv("ACTIVE_EXCHANGES", "binance")
    monkeypatch.setenv("BINANCE_API_KEY", "k")
    monkeypatch.setenv("BINANCE_API_SECRET", "s")
    import loop.runner as r
    from core.config import list_gateways
    r.start_runner()
    registered = list_gateways()
    assert {(m, x) for m, x, _ in registered} == {("paper", "binance"), ("live", "binance")}
