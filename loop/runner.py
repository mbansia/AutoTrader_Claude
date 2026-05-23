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

# Heartbeat: log one INFO event per (mode, exchange) per interval so
# diagnostics' `seconds_since_last_event` stays below the 3600s threshold
# on a healthy loop that never hits errors or warnings.
_HEARTBEAT_INTERVAL_S = 3600
_last_heartbeat: dict[tuple[str, str], float] = {}


def _heartbeat_if_due(mode: str, exchange_id: str) -> None:
    key = (mode, exchange_id)
    now = time.time()
    if now - _last_heartbeat.get(key, 0.0) >= _HEARTBEAT_INTERVAL_S:
        _last_heartbeat[key] = now
        try:
            with session_scope() as session:
                log_event(
                    session,
                    mode=mode,
                    exchange=exchange_id,
                    level="INFO",
                    message="loop heartbeat",
                )
        except Exception:  # noqa: BLE001
            pass


# Per §6.4 + L11, Hyperliquid is opt-in: the gateway code is in repo but
# trading on it requires explicit operator activation since (a) the funding
# math at hourly cadence behaves differently from CEX 4h/8h, and (b) EVM
# private-key compromise is catastrophic and needs a deliberate decision.
def _exchanges_to_activate(env: EnvConfig) -> list[ExchangeId]:
    """Read activation from the DB (`venue_state.active`). Per-exchange
    toggles live in the dashboard (`/safety`) — no env var needed.

    Back-compat: if the deprecated `ACTIVE_EXCHANGES` env var is set, it
    one-shot mirrors into the DB on this boot. Subsequent boots use the
    DB authoritatively; the env var becomes a no-op.

    A venue is activated only if BOTH conditions hold:
      (a) `venue_state.active = true`
      (b) credentials are present in env for that venue
    Credential presence is a deployment-surface check; even if the
    dashboard says "active", the bot won't try to construct a live
    gateway with empty credentials.
    """
    from sqlalchemy import select
    from state import session_scope
    from state.models import VenueState

    legacy = os.environ.get("ACTIVE_EXCHANGES", "").strip()
    legacy_set: set[str] = (
        {x.strip() for x in legacy.split(",") if x.strip()} if legacy else set()
    )

    out: list[ExchangeId] = []
    with session_scope() as session:
        rows = list(session.scalars(select(VenueState)))
        for row in rows:
            # Apply ACTIVE_EXCHANGES override exactly once: if the env var
            # is set and disagrees with the DB row, write through.
            if legacy_set and (row.exchange_id in legacy_set) != row.active:
                row.active = row.exchange_id in legacy_set
                session.flush()
            if not row.active:
                continue
            if not _has_credentials(row.exchange_id, env):
                log.warning(
                    "venue %s active=true in DB but credentials missing in env",
                    row.exchange_id,
                )
                continue
            out.append(row.exchange_id)  # type: ignore[arg-type]
    return out


def _has_credentials(exchange_id: str, env: EnvConfig) -> bool:
    if exchange_id == "binance":
        return bool(env.binance_api_key and env.binance_api_secret)
    if exchange_id == "kucoin":
        return bool(env.kucoin_api_key and env.kucoin_api_secret and env.kucoin_passphrase)
    if exchange_id == "hyperliquid":
        return bool(env.hyperliquid_wallet_address and env.hyperliquid_private_key)
    return False


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
    """Source-of-truth order for the expected id:
       1. `venue_state.expected_account_id` in the DB (set via /safety)
       2. The gateway's `expected_account_id()` (the env var)
       3. Neither — log only, do not assert (first-boot soft path; operator
          locks the value in via /safety once they've reviewed it)

    On boot, if (1) and (2) are both empty we still PROBE the actual id
    and log it INFO so the operator has it ready to paste into /safety.
    The boot proceeds regardless.
    """
    from state import session_scope
    from state.repository import get_venue_state

    expected_db = ""
    with session_scope() as session:
        row = get_venue_state(session, gw.exchange_id)
        if row is not None:
            expected_db = row.expected_account_id.strip().lower()

    expected = expected_db or gw.expected_account_id().strip().lower()

    try:
        actual = gw.actual_account_id().strip().lower()
    except Exception as exc:  # noqa: BLE001
        log.warning("account-id probe failed for %s: %r", gw.exchange_id, exc)
        return

    if not actual:
        log.warning("account-id probe returned empty for %s", gw.exchange_id)
        return

    if not expected:
        log.info(
            "account-id detected for %s: %s (unlocked — set via /safety to assert on boot)",
            gw.exchange_id, actual,
        )
        return

    if actual != expected:
        raise AccountIdMismatch(
            f"{gw.exchange_id}: expected account {expected!r} but venue "
            f"reports {actual!r} — refusing to start. Fix via /safety "
            f"(or unset the venue_state.expected_account_id to re-detect)."
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
        _heartbeat_if_due(mode, exchange_id)
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
    # Test / dev escape hatch only — defaults to on so production doesn't
    # need to think about it. Set BOT_WORKER_ENABLED=0 to serve the UI
    # without spawning trading workers (used by the parity harness).
    if os.environ.get("BOT_WORKER_ENABLED", "1") == "0":
        log.info("BOT_WORKER_ENABLED=0 — runner skipped (UI-only mode)")
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
