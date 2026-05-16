"""Background loop runner. Starts one thread per (mode, exchange) and calls
`run_cycle` every `loop_seconds`.

Wired into the FastAPI app's startup event. Honors `BOT_WORKER_ENABLED=0`
for API-only replicas. Graceful SIGTERM via a stop event each thread polls.

Activation order:
  1. read env → build live gateways for every venue whose creds are present
  2. always build paper gateways for those same venues (mirrored state)
  3. register everything in core.config registry (diagnostics + UI use it)
  4. spawn one thread per (mode, venue) → run_cycle in a sleep loop
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Literal

from core.config import (
    EnvConfig,
    ExchangeId,
    clear_registry,
    list_gateways,
    load_env,
    register_gateway,
)
from core.types import MergedConfig, Mode
from gateways import build_live_gateway
from gateways.base import Gateway
from gateways.paper import InMemoryGateway
from state import init_db, session_scope
from state.repository import (
    get_or_create_mode_state,
    get_or_create_strategy_config,
    get_or_seed_per_strategy_config,
    log_event,
)


log = logging.getLogger(__name__)


# Per §6.4 + L11, Hyperliquid is opt-in: the gateway code is in repo but
# trading on it requires explicit operator activation since (a) the funding
# math at hourly cadence behaves differently from CEX 4h/8h, and (b) EVM
# private-key compromise is catastrophic and needs a deliberate decision.
def _exchanges_to_activate(env: EnvConfig) -> list[ExchangeId]:
    raw = os.environ.get("ACTIVE_EXCHANGES", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]  # type: ignore[return-value]
    # Default: activate any venue whose creds are provisioned, EXCEPT
    # hyperliquid (must be opted in by setting ACTIVE_EXCHANGES explicitly).
    out: list[ExchangeId] = []
    if env.binance_api_key and env.binance_api_secret:
        out.append("binance")
    if env.kucoin_api_key and env.kucoin_api_secret and env.kucoin_passphrase:
        out.append("kucoin")
    return out


def _trade_type_for(exchange_id: ExchangeId) -> str:
    return f"{exchange_id}_same_venue_funding_arb"


@dataclass
class WorkerHandle:
    mode: Mode
    exchange_id: ExchangeId
    thread: threading.Thread
    stop_event: threading.Event


_workers: list[WorkerHandle] = []


class AccountIdMismatch(Exception):
    """Boot-time guard per §3.1 mitigation policy: refuse to start if the
    venue's actual account id doesn't match the operator's expected env
    var. Eliminates the sub-account / wrong-wallet misconfig class.
    """


def _assert_account_id(gw: Gateway) -> None:
    expected = gw.expected_account_id().strip().lower()
    if not expected:
        # No expected id configured → cannot assert; skip (operator opted out).
        return
    try:
        actual = gw.actual_account_id().strip().lower()
    except Exception as exc:  # noqa: BLE001
        log.warning("account-id probe failed for %s: %r", gw.exchange_id, exc)
        return
    if not actual:
        log.warning("account-id probe returned empty for %s", gw.exchange_id)
        return
    if actual != expected:
        raise AccountIdMismatch(
            f"{gw.exchange_id}: expected account {expected!r} but venue "
            f"reports {actual!r} — refusing to start. Check the *_EXPECTED_"
            f"ACCOUNT_ID env var (and on Hyperliquid: the wallet address "
            f"itself is the account)."
        )


def _build_gateway(exchange_id: ExchangeId, *, env: EnvConfig, mode: Mode) -> Gateway:
    """Live gateway in live mode; in-memory gateway in paper mode (per-venue).
    Paper-mode gateway mirrors the live venue's exchange_id so diagnostics +
    UI surface the same per-venue cards for both modes.
    """
    if mode == "live":
        return build_live_gateway(exchange_id, env=env)
    return InMemoryGateway(exchange_id=exchange_id)


def _run_one(gw: Gateway, mode: Mode, trade_type: str) -> None:
    """Single cycle inside its own session_scope so a crash stays bounded."""
    from loop.cycle import run_cycle

    with session_scope() as session:
        cfg_row = get_or_seed_per_strategy_config(session, trade_type)
        glob_row = get_or_create_strategy_config(session)
        # Build a MergedConfig from the persisted rows (§4 layered config).
        cfg = MergedConfig(
            entry_min_net_apy=cfg_row.entry_min_net_apy,
            exit_min_net_apy=cfg_row.exit_min_net_apy,
            exit_basis_buffer_multiple=cfg_row.exit_basis_buffer_multiple,
            max_exit_basis_bps=cfg_row.max_exit_basis_bps,
            stop_loss_pct=cfg_row.stop_loss_pct,
            basis_dislocation_exit_bps=cfg_row.basis_dislocation_exit_bps,
            min_position_pct=cfg_row.min_position_pct,
            max_position_pct=cfg_row.max_position_pct,
            sub_target_sizing_factor=cfg_row.sub_target_sizing_factor,
            perp_leverage=cfg_row.perp_leverage,
            max_perp_leverage=cfg_row.max_perp_leverage,
            auto_transfer_enabled=cfg_row.auto_transfer_enabled,
            auto_quote_swap_enabled=cfg_row.auto_quote_swap_enabled,
            futures_buffer_pct=cfg_row.futures_buffer_pct,
            depeg_guard_bps=cfg_row.depeg_guard_bps,
            max_open_positions=glob_row.max_open_positions,
            max_trades_per_day=glob_row.max_trades_per_day,
            loop_seconds=glob_row.loop_seconds,
            paper_starting_equity=glob_row.paper_starting_equity,
            paper_slippage_bps=glob_row.paper_slippage_bps,
            paper_fee_bps=glob_row.paper_fee_bps,
            hedge_integrity_check=glob_row.hedge_integrity_check,
            delisting_check=glob_row.delisting_check,
            cycle_error_rate_threshold=glob_row.cycle_error_rate_threshold,
            slippage_alert_bps=glob_row.slippage_alert_bps,
        )
        run_cycle(
            session=session,
            gateway=gw,
            mode=mode,
            config=cfg,
            trade_type=trade_type,
        )


def _worker_loop(
    *,
    mode: Mode,
    exchange_id: ExchangeId,
    gateway: Gateway,
    trade_type: str,
    stop_event: threading.Event,
) -> None:
    log.info("loop start: mode=%s venue=%s trade_type=%s", mode, exchange_id, trade_type)
    while not stop_event.is_set():
        cycle_started = time.monotonic()
        try:
            _run_one(gateway, mode, trade_type)
        except Exception as exc:  # noqa: BLE001
            # §3.1 SOP "Crash handling" — log ERROR + continue. run_cycle
            # already catches inside; this is the runner-level safety net.
            log.exception("worker_loop_crash: mode=%s venue=%s", mode, exchange_id)
            try:
                with session_scope() as session:
                    log_event(
                        session,
                        mode=mode,
                        exchange=exchange_id,
                        level="ERROR",
                        message=f"runner_crash: {exc!r}",
                    )
            except Exception:  # noqa: BLE001
                pass
        # Sleep `loop_seconds` minus elapsed, but never less than 1s and
        # never more than the configured value. Re-read config every cycle
        # so live edits take effect on the next iteration.
        try:
            with session_scope() as session:
                cfg = get_or_create_strategy_config(session)
                period = max(1, int(cfg.loop_seconds))
        except Exception:  # noqa: BLE001
            period = 30
        elapsed = time.monotonic() - cycle_started
        remaining = max(0.5, period - elapsed)
        if stop_event.wait(remaining):
            break
    log.info("loop stop: mode=%s venue=%s", mode, exchange_id)


def start_runner() -> list[WorkerHandle]:
    """Spawn workers per env + persistent config. Idempotent — calling
    twice is a no-op (returns the existing handles). Returns the list of
    spawned workers so tests / shutdown can join them.
    """
    if _workers:
        return list(_workers)
    if os.environ.get("BOT_WORKER_ENABLED", "1") == "0":
        log.info("BOT_WORKER_ENABLED=0 — runner skipped")
        return []
    env = load_env()
    init_db()
    active = _exchanges_to_activate(env)
    if not active:
        log.warning("no exchanges activated — set creds or ACTIVE_EXCHANGES env var")
        return []

    # Seed singletons up front so the loop's first iteration doesn't race
    # on lazy seed (multi-worker start would deadlock on strategy_config id=1).
    with session_scope() as session:
        get_or_create_strategy_config(session)
        for mode in ("paper", "live"):
            get_or_create_mode_state(session, mode)
        for exchange_id in active:
            get_or_seed_per_strategy_config(session, _trade_type_for(exchange_id))

    for exchange_id in active:
        for mode in ("paper", "live"):
            if mode == "live" and exchange_id == "hyperliquid":
                # Extra explicit safety: HL live requires both an explicit
                # ACTIVE_EXCHANGES opt-in AND a non-empty private key.
                if not env.hyperliquid_private_key:
                    log.warning("hyperliquid live skipped: no private key")
                    continue
            try:
                gw = _build_gateway(exchange_id, env=env, mode=mode)
                # Live mode: assert wallet/account id matches operator expectation
                # BEFORE registering or spawning a worker. Paper mode is safe
                # (synthetic gateway returns its own configured id).
                if mode == "live":
                    _assert_account_id(gw)
                register_gateway(mode, exchange_id, gw)
                stop_event = threading.Event()
                t = threading.Thread(
                    target=_worker_loop,
                    name=f"loop-{mode}-{exchange_id}",
                    kwargs={
                        "mode": mode,
                        "exchange_id": exchange_id,
                        "gateway": gw,
                        "trade_type": _trade_type_for(exchange_id),
                        "stop_event": stop_event,
                    },
                    daemon=True,
                )
                t.start()
                _workers.append(
                    WorkerHandle(mode=mode, exchange_id=exchange_id, thread=t, stop_event=stop_event)
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("failed to start worker mode=%s venue=%s: %r", mode, exchange_id, exc)
    log.info("started %d workers: %s", len(_workers), [(w.mode, w.exchange_id) for w in _workers])
    return list(_workers)


def stop_runner(timeout_s: float = 10.0) -> None:
    """Signal every worker to stop + wait up to `timeout_s` for each to
    drain its current cycle. SIGTERM-safe per §3.1 mitigation policy.
    """
    for w in _workers:
        w.stop_event.set()
    deadline = time.monotonic() + timeout_s
    for w in _workers:
        remaining = max(0.0, deadline - time.monotonic())
        w.thread.join(timeout=remaining)
    _workers.clear()
    clear_registry()
