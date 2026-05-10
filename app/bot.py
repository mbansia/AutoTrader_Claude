"""Bot loop and per-cycle business logic.

This module is the heart of the funding-rate arbitrage strategy. The
top-level functions are:

* :func:`run_loop` — long-running worker that calls :func:`run_one_cycle`
  on a timer. Started as a daemon thread by ``app.main`` on FastAPI
  startup (or by an external worker process).
* :func:`run_one_cycle` — runs one pass for paper, live, or both. It
  delegates each pass to :func:`run_one_cycle_for_mode`.
* :func:`run_one_cycle_for_mode` — the actual cycle: maintenance →
  reconcile → safety → exits → entry → rebalance → snapshot.

Helper functions are grouped roughly by responsibility:

* State accessors: :func:`get_runtime_state`, :func:`get_mode_state`,
  :func:`get_strategy_config`.
* Closing: :func:`_force_close_both`, :func:`_close_naked_leg`,
  :func:`_ensure_close_readiness`, :func:`_actual_spot_qty`,
  :func:`_actual_perp_qty`, :func:`manual_close`.
* Capital provisioning: spot↔futures top-up before close,
  :func:`_take_balance_snapshot`.
* Paper-mode simulation: :func:`_accrue_paper_funding`.
* Reconciliation: :func:`reconcile_positions` (orphan perp →
  rehydrate), :func:`_reconcile_open_position_state` (DB open but
  Binance flat → mark closed).

Module-level state (intentional, called out explicitly):

* :data:`_LIVE_API_UNHEALTHY_LOGGED` — single bool guarding the
  "live API down" ERROR log so we only emit it once per outage
  rather than every cycle. Reset on the next successful probe.
* :data:`_CLOSE_ERROR_CACHE` — per ``(position_id, leg)`` cache of the
  last error message, used to dedup close-failure logs across
  cycles. Cleared on a successful close. Both are module-level
  because they survive across ``run_one_cycle`` invocations within
  the same Python process.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.events import bus
from app.exchange import VenueGateway, _interval_hours, annualize_rate, make_gateways
from app.finance import position_realized_pnl, position_unrealized_pnl, total_realized_pnl
from app.models import (
    ALL_MODES,
    MODE_LIVE,
    MODE_PAPER,
    VENUE_BINANCE,
    BalanceSnapshot,
    BotEvent,
    CapitalFlow,
    EquityCurve,
    ModeState,
    Position,
    RejectedCandidate,
    RuntimeState,
    ScanResult,
    StrategyConfig,
    Trade,
    venue_to_trade_type,
)
from app.safety import (
    check_hedge,
    check_market_health,
    is_basis_entry_acceptable,
    is_basis_exit_favorable,
)


# Module-level dedup so we only log "live API down" once per outage instead of every cycle.
_LIVE_API_UNHEALTHY_LOGGED = False

# Per-position close error dedup. Same (position_id, leg, error_msg) tuple suppresses
# a duplicate ERROR row on consecutive cycles. Cleared when the close succeeds.
_CLOSE_ERROR_CACHE: dict[tuple[int, str], str] = {}


def log_event(db, message: str, mode: str = MODE_PAPER, level: str = 'INFO', exchange: str = 'system'):
    """Persist a BotEvent row.

    ``exchange`` should be the venue this event pertains to (``binance``,
    ``kucoin``, …) so the Logs tab can filter by venue. Use ``'system'`` for
    cross-venue events (e.g. cycle-level errors, schema migrations) so they
    don't get attributed to a single venue."""
    db.add(BotEvent(mode=mode, exchange=exchange, level=level, message=message))


def get_runtime_state(db) -> RuntimeState:
    state = db.scalar(select(RuntimeState).where(RuntimeState.id == 1))
    if state is None:
        state = RuntimeState(id=1, paper_mode=True, maintenance_mode=False)
        db.add(state)
        db.flush()
    return state


def get_mode_state(db, mode: str) -> ModeState:
    state = db.scalar(select(ModeState).where(ModeState.mode == mode))
    if state is None:
        # Default: paper enabled, live disabled until the user explicitly turns it on.
        state = ModeState(mode=mode, entry_enabled=(mode == MODE_PAPER), exit_enabled=True, maintenance_mode=False)
        db.add(state)
        db.flush()
    return state


def get_strategy_state(db, mode: str, trade_type: str) -> 'StrategyState':
    """Per-(mode, trade_type) entry switch. Bootstrap: the same-venue
    funding-arb strategies start enabled in paper / disabled in live;
    cross-venue and onchain strategies start disabled until their
    orchestrators are wired."""
    from app.models import StrategyState as _SS
    s = db.scalar(select(_SS).where(_SS.mode == mode, _SS.trade_type == trade_type))
    if s is None:
        same_venue = trade_type in ('binance_same_venue_funding_arb', 'kucoin_same_venue_funding_arb')
        s = _SS(
            mode=mode,
            trade_type=trade_type,
            entry_enabled=(mode == MODE_PAPER and same_venue),
            exit_all_pending=False,
        )
        db.add(s)
        db.flush()
    return s


def venue_is_active(db, venue_id: str) -> bool:
    """True iff at least one strategy that uses ``venue_id`` as a leg has
    ``entry_enabled=True`` in either mode. When this returns False, the
    cycle and dashboard skip all API calls to the venue — there's no
    work to do, and not calling preserves rate-limit budget for venues
    that ARE active. As soon as the operator re-enables any strategy
    on /config, the next cycle picks up immediately."""
    from app.models import StrategyState as _SS
    from app.models import trade_types_touching_venue as _tt
    relevant = _tt(venue_id)
    if not relevant:
        return True  # unknown venue — fail-open (don't accidentally block)
    rows = db.scalars(select(_SS).where(_SS.trade_type.in_(relevant))).all()
    if not rows:
        # No state rows yet (fresh install); default-enabled strategies
        # exist conceptually, so treat as active. The first cycle will
        # bootstrap rows via get_strategy_state.
        return True
    return any(r.entry_enabled for r in rows)


def _accrue_paper_funding(db, gateway, mode: str) -> None:
    """Linearly accrue funding income on every open paper position based on the wall-clock
    elapsed since last accrual. Funding payments are real cash that lands in the trader's
    wallet — credit them so the next cycle's % sizing can deploy the larger equity.
    Scoped to ``gateway.venue_id`` so multi-venue paper books don't double-accrue."""
    if mode != MODE_PAPER:
        return
    now = datetime.utcnow()
    for p in db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode, Position.exchange == gateway.venue_id)).all():
        last = p.last_funding_accrual_ts or p.opened_at
        elapsed_seconds = (now - last).total_seconds()
        if elapsed_seconds <= 0:
            continue
        interval_h = p.funding_interval_hours or 8.0
        period_seconds = interval_h * 3600.0
        if period_seconds <= 0:
            continue
        # Linear accrual within a period — the per-second slice of one
        # funding payment. Real Binance funding is computed on the
        # mark price × qty, not the entry price; using entry-price
        # diverges from live by 5%+ over weeks of accrual on a position
        # whose mark drifts. We pull current mark via gateway's price
        # cache (already 30s-cached, no extra API calls in steady state).
        try:
            from app.exchange import make_gateways
            _gws = make_gateways()
            _gw = next((g for g in _gws if g.venue_id == p.exchange), None)
            mark_px = _gw.safe_price(p.perp_symbol, perp=True) if _gw else None
            ref_px = float(mark_px) if mark_px else float(p.spot_entry_price or 0)
        except Exception:
            ref_px = float(p.spot_entry_price or 0)
        notional = ref_px * p.quantity
        income = notional * p.last_funding_rate * (elapsed_seconds / period_seconds)
        p.funding_income_accrued += income
        p.last_funding_accrual_ts = now


_LEGACY_PERIOD_SENTINEL = 0.005


def get_strategy_config(db) -> StrategyConfig:
    cfg = db.scalar(select(StrategyConfig).where(StrategyConfig.id == 1))
    if cfg is None:
        cfg = StrategyConfig(id=1)
        db.add(cfg)
        db.flush()
        return cfg
    # One-shot legacy threshold migration, gated by a PERSISTED schema
    # version so it runs exactly once per row regardless of how many
    # times get_strategy_config() is called and regardless of any
    # subsequent threshold edits. Old semantic was "per-period rate"
    # (e.g. 0.0001 = 1 bp per funding window); new semantic is
    # "annualized compounded APR" (e.g. 0.10 = 10% APY). Runs at v0
    # and bumps to v1.
    if (cfg.config_schema_version or 0) < 1:
        before_entry = cfg.entry_funding_threshold
        before_exit = cfg.exit_funding_threshold
        if 0 < cfg.entry_funding_threshold < _LEGACY_PERIOD_SENTINEL:
            cfg.entry_funding_threshold = round(cfg.entry_funding_threshold * 1095.0, 6)
        if 0 < cfg.exit_funding_threshold < _LEGACY_PERIOD_SENTINEL:
            cfg.exit_funding_threshold = round(cfg.exit_funding_threshold * 1095.0, 6)
        cfg.config_schema_version = 1
        if before_entry != cfg.entry_funding_threshold or before_exit != cfg.exit_funding_threshold:
            log_event(db, f'Migrated funding thresholds to APR (v0→v1): entry {before_entry:.6f}→{cfg.entry_funding_threshold:.4f}, exit {before_exit:.6f}→{cfg.exit_funding_threshold:.4f}', mode=MODE_PAPER, exchange='system')
        db.flush()
    return cfg


def record_trade(db, position_id: int | None, mode: str, symbol: str, venue: str, side: str, qty: float, order: dict, exchange: str = VENUE_BINANCE, trade_type: str | None = None):
    """Record a single fill. ``venue`` is the leg ('spot' / 'futures').
    ``exchange`` is the broker the fill happened on (Binance, KuCoin, IBKR…)
    — the system treats the whole portfolio as a single pool distributed
    across these exchanges. ``trade_type`` mirrors the parent position's
    strategy tag; defaults to the same-venue funding-arb derived from the
    exchange when not supplied (back-compat)."""
    db.add(Trade(
        mode=mode,
        exchange=exchange,
        trade_type=trade_type or venue_to_trade_type(exchange),
        position_id=position_id,
        symbol=symbol,
        venue=venue,
        side=side,
        quantity=qty,
        price=float(order.get('price') or 0),
        fee=float((order.get('fee') or {}).get('cost') or 0),
    ))


def _rollback_spot(db, gateway: VenueGateway, pos: Position, c, qty: float, paper: bool, cfg: StrategyConfig) -> None:
    """When the perp leg fails after spot bought, sell the spot back via
    a limit-IOC at bid − tick buffer. If THAT fails, log critical and
    leave the position open with naked-long exposure for the next-cycle
    hedge check to repair. Mirrors the close path's protected-exec
    convention so we never use a bare market order in live."""
    try:
        sim = gateway.simulate_fill(c.spot_symbol, qty, side='sell', perp=False)
        tick_bps = float(cfg.entry_tick_buffer_bps or 1.0)
        limit = sim['worst_price'] * (1 - tick_bps / 10000.0) if sim['worst_price'] > 0 else 0
        if limit <= 0:
            # Book empty / unreachable — fall back to a market sell so we
            # at least try to flatten. Worse slippage but avoids carrying
            # naked long exposure into the next cycle.
            rev = gateway.close_spot(c.spot_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
        else:
            rev = gateway.close_spot_limit_ioc(c.spot_symbol, qty, limit, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
        record_trade(db, pos.id, pos.mode, c.spot_symbol, 'spot', 'sell', qty, rev, exchange=gateway.venue_id)
        pos.status = 'closed'
        pos.closed_at = datetime.utcnow()
        log_event(db, f'Rolled back spot leg of {c.perp_symbol} after perp failure; loss ≈ entry+exit fees + slippage', mode=pos.mode, exchange=gateway.venue_id)
    except Exception as e:
        log_event(db, f'CRITICAL: spot rollback failed: {str(e)[:140]}. {c.spot_symbol} is naked-long; hedge check will retry next cycle.', mode=pos.mode, level='ERROR', exchange=gateway.venue_id)


def _log_close_error(db, p: Position, leg: str, err: str) -> None:
    """Record a close-leg error, but only if it's different from the last one we
    logged for this (position, leg). Stops the same 'Margin is insufficient'
    line from spamming the Logs tab on every cycle. Also persists the latest
    error on the Position row so the UI can show it without diving into Logs."""
    key = (p.id, leg)
    snippet = err[:140]
    p.last_close_error = f'{leg}: {snippet}'
    if _CLOSE_ERROR_CACHE.get(key) == snippet:
        return  # already logged this exact error for this leg
    _CLOSE_ERROR_CACHE[key] = snippet
    log_event(db, f'Failed to close {leg} leg of {p.perp_symbol}: {snippet} (will retry next cycle)', mode=p.mode, level='ERROR', exchange=p.exchange)


def _ensure_close_readiness(db, gateway: VenueGateway, p: Position, cfg: StrategyConfig) -> None:
    """Reshuffle the position's quote-currency spot↔futures so the close
    has both legs funded. Live only — paper has no separate wallets.
    Uses ``p.quote_currency`` so a USDC position pulls from USDC free
    balance, not USDT (and vice versa) — never cross-funded."""
    if p.mode != MODE_LIVE:
        return

    quote = p.quote_currency or 'USDT'
    bals = gateway.safe_balances() or {}
    fut_free = float((bals.get('futures', {}).get(quote) or {}).get('free') or 0)
    spot_free = float((bals.get('spot', {}).get(quote) or {}).get('free') or 0)
    perp_now = gateway.safe_price(p.perp_symbol, perp=True) or p.perp_entry_price or 0.0
    if perp_now <= 0:
        return
    leverage = max(1, cfg.max_perp_leverage or cfg.perp_leverage or 1)
    needed_margin = p.quantity * perp_now / leverage
    target = needed_margin * 1.005
    if fut_free >= target:
        return

    gap = target - fut_free
    if cfg.auto_transfer_enabled and spot_free > 0.30:
        avail = max(0.0, spot_free - 0.10)
        transfer = min(gap, avail)
        if transfer >= 0.20:
            ok, err = gateway.transfer_spot_to_futures(transfer, False, asset=quote)
            if ok:
                log_event(db, f'Pre-close: transferred {transfer:.2f} {quote} spot→futures (margin top-up for {p.perp_symbol})', mode=p.mode, exchange=p.exchange)
            else:
                log_event(db, f'Pre-close: spot→futures {quote} transfer for {p.perp_symbol} failed: {err[:120]}', mode=p.mode, level='WARN', exchange=p.exchange)


def _actual_spot_qty(gateway: VenueGateway, p: Position) -> float:
    """Actual base-asset balance on Binance spot, capped at p.quantity. Handles
    drift from fee deductions, lot-size rounding, or earlier partial fills."""
    bals = gateway.safe_balances() or {}
    base = p.spot_symbol.split('/')[0]
    actual = float((bals.get('spot', {}).get(base) or {}).get('total') or 0)
    return min(p.quantity, max(0.0, actual))


def _actual_perp_qty(gateway: VenueGateway, p: Position) -> float:
    """Actual perp position size on Binance, capped at p.quantity."""
    try:
        for r in gateway.open_perp_positions_raw():
            if r.get('symbol') == p.perp_symbol:
                actual = abs(float(r.get('contracts') or r.get('info', {}).get('positionAmt') or 0))
                return min(p.quantity, max(0.0, actual))
    except Exception:
        pass
    return 0.0


def _is_dust(qty: float, min_amount: float) -> bool:
    """True if `qty` is below the symbol's LOT_SIZE minimum — i.e., we can't
    place a market order for it. Treated as "already flat" by the close path
    so the bot doesn't spin on un-tradeable dust."""
    return min_amount > 0 and 0 < qty < min_amount


def _position_leg_states(gateway: VenueGateway, p: Position, tolerance: float = 0.05) -> dict:
    """Returns per-leg state vs Binance for a position. Used for UI display
    and the cycle's reconciliation step.

    spot_alive / perp_alive: True if the actual quantity on Binance is at
    least (1 - tolerance) × p.quantity AND above the symbol's LOT_SIZE
    minimum. Tolerance accounts for fee/lot-size drift; the min-lot check
    ensures sub-min dust (which Binance refuses to trade) counts as flat
    so the bot doesn't loop on un-closeable positions."""
    spot_actual = _actual_spot_qty(gateway, p)
    perp_actual = _actual_perp_qty(gateway, p)
    spot_min = gateway.market_min_amount(p.spot_symbol, perp=False)
    perp_min = gateway.market_min_amount(p.perp_symbol, perp=True)
    threshold = p.quantity * (1.0 - tolerance)
    spot_alive = spot_actual >= threshold and not _is_dust(spot_actual, spot_min)
    perp_alive = perp_actual >= threshold and not _is_dust(perp_actual, perp_min)
    return {
        'spot_actual': spot_actual,
        'perp_actual': perp_actual,
        'spot_alive': spot_alive,
        'perp_alive': perp_alive,
        'spot_min': spot_min,
        'perp_min': perp_min,
    }


def _reconcile_open_position_state(db, gateway: VenueGateway, mode: str) -> None:
    """For each open position on this gateway's venue, check actual leg state.
    If both legs are flat (e.g., the user closed manually, or a previous
    partial close fully completed externally), mark the row closed in DB so
    the UI matches reality. Live mode only."""
    if mode != MODE_LIVE:
        return
    for p in db.scalars(select(Position).where(Position.status == 'open', Position.mode == MODE_LIVE, Position.exchange == gateway.venue_id)).all():
        st = _position_leg_states(gateway, p)
        if not st['spot_alive'] and not st['perp_alive']:
            p.status = 'closed'
            p.closed_at = datetime.utcnow()
            p.last_close_error = ''
            _CLOSE_ERROR_CACHE.pop((p.id, 'spot'), None)
            _CLOSE_ERROR_CACHE.pop((p.id, 'perp'), None)
            log_event(db, f'Reconciled {p.perp_symbol} on {gateway.name}: both legs flat — marking closed', mode=mode, exchange=gateway.venue_id)


def _force_close_both(db, gateway: VenueGateway, p: Position, cfg: StrategyConfig, reason: str) -> None:
    """Close both legs of a position. The whole point of an arb is being hedged,
    so we never WANT to leave one leg open — if either close fails we either
    abort (preserving the hedge for retry) or surface CRITICAL when there's no
    safe path back.

    Order: spot first (sell the base asset), then perp (buy back the short).
    If spot fails, abort entirely so the perp short stays in place as the
    hedge for the unsold spot. If spot succeeds but perp fails, we briefly
    have naked-short exposure; the hedge check on the next cycle will buy
    back the perp via _close_naked_leg.

    Quantities are pulled from actual Binance balances (capped at the DB
    quantity), so drift from fees / lot-size rounding doesn't cause
    "insufficient balance" rejections — we sell exactly what we have."""
    paper = (p.mode == MODE_PAPER)
    if not paper:
        _ensure_close_readiness(db, gateway, p, cfg)

    if paper:
        spot_qty = perp_qty = p.quantity
        spot_min = perp_min = 0.0
    else:
        spot_qty = _actual_spot_qty(gateway, p)
        perp_qty = _actual_perp_qty(gateway, p)
        spot_min = gateway.market_min_amount(p.spot_symbol, perp=False)
        perp_min = gateway.market_min_amount(p.perp_symbol, perp=True)

    spot_ok = (spot_qty <= 0.0)  # nothing to sell counts as already-flat
    perp_ok = (perp_qty <= 0.0)

    # Sub-LOT_SIZE dust is un-tradeable on Binance — treat the leg as flat.
    if _is_dust(spot_qty, spot_min):
        log_event(db, f'Spot leg of {p.perp_symbol} is sub-min-lot dust ({spot_qty:.6f} < {spot_min:.6f}); treating as flat', mode=p.mode, level='WARN', exchange=p.exchange)
        spot_qty = 0.0
        spot_ok = True
    if _is_dust(perp_qty, perp_min):
        log_event(db, f'Perp leg of {p.perp_symbol} is sub-min-lot dust ({perp_qty:.6f} < {perp_min:.6f}); treating as flat', mode=p.mode, level='WARN', exchange=p.exchange)
        perp_qty = 0.0
        perp_ok = True

    # ---- Spot leg first ----
    if spot_qty > 0:
        try:
            s = gateway.close_spot(p.spot_symbol, spot_qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
            record_trade(db, p.id, p.mode, p.spot_symbol, 'spot', 'sell', spot_qty, s, exchange=p.exchange)
            spot_ok = True
        except Exception as e:
            _log_close_error(db, p, 'spot', str(e))
            # Abort — leaving perp open keeps the position fully hedged for retry.
            return

    # ---- Perp leg ----
    if perp_qty > 0:
        try:
            f = gateway.close_perp(p.perp_symbol, perp_qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
            record_trade(db, p.id, p.mode, p.perp_symbol, 'futures', 'buy', perp_qty, f, exchange=p.exchange)
            perp_ok = True
        except Exception as e:
            _log_close_error(db, p, 'perp', str(e))
            if spot_qty > 0 and spot_ok:
                # Spot already sold, perp failed → naked-short exposure. Hedge
                # check next cycle will call _close_naked_leg(surviving='perp')
                # which buys back the perp.
                log_event(
                    db,
                    f'CRITICAL: spot leg of {p.perp_symbol} sold but perp buy-back failed — naked-short exposure until hedge check resolves it next cycle',
                    mode=p.mode, level='ERROR',
                )

    if spot_ok and perp_ok:
        p.status = 'closed'
        p.closed_at = datetime.utcnow()
        p.last_close_error = ''
        _CLOSE_ERROR_CACHE.pop((p.id, 'spot'), None)
        _CLOSE_ERROR_CACHE.pop((p.id, 'perp'), None)
        realized = position_realized_pnl(db, p)
        log_event(db, f'Closed {p.perp_symbol} ({reason}); realized={realized:+.4f}', mode=p.mode, exchange=p.exchange)
        bus.emit('position_closed',
                 position_id=p.id, mode=p.mode, exchange=p.exchange,
                 symbol=p.symbol, realized_usdt=realized)


def _close_naked_leg(db, gateway: VenueGateway, p: Position, cfg: StrategyConfig, surviving_leg: str | None, reason: str) -> None:
    paper = (p.mode == MODE_PAPER)
    closed_ok = False
    try:
        if surviving_leg == 'spot':
            if not paper:
                _ensure_close_readiness(db, gateway, p, cfg)
            qty = _actual_spot_qty(gateway, p) if not paper else p.quantity
            min_amt = gateway.market_min_amount(p.spot_symbol, perp=False) if not paper else 0.0
            if _is_dust(qty, min_amt):
                log_event(db, f'Naked spot leg of {p.perp_symbol} is sub-min-lot dust ({qty:.6f} < {min_amt:.6f}); treating as flat', mode=p.mode, level='WARN', exchange=p.exchange)
                closed_ok = True
            elif qty <= 0:
                closed_ok = True
            else:
                s = gateway.close_spot(p.spot_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                record_trade(db, p.id, p.mode, p.spot_symbol, 'spot', 'sell', qty, s, exchange=p.exchange)
                closed_ok = True
        elif surviving_leg == 'perp':
            qty = _actual_perp_qty(gateway, p) if not paper else p.quantity
            min_amt = gateway.market_min_amount(p.perp_symbol, perp=True) if not paper else 0.0
            if _is_dust(qty, min_amt):
                log_event(db, f'Naked perp leg of {p.perp_symbol} is sub-min-lot dust ({qty:.6f} < {min_amt:.6f}); treating as flat', mode=p.mode, level='WARN', exchange=p.exchange)
                closed_ok = True
            elif qty <= 0:
                closed_ok = True
            else:
                f = gateway.close_perp(p.perp_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                record_trade(db, p.id, p.mode, p.perp_symbol, 'futures', 'buy', qty, f, exchange=p.exchange)
                closed_ok = True
        else:
            closed_ok = True  # no surviving leg to close
    except Exception as e:
        snippet = str(e)[:140]
        p.last_close_error = f'{surviving_leg or "?"}: {snippet}'
        log_event(db, f'Failed to flatten naked {surviving_leg} leg of {p.perp_symbol}: {snippet} (will retry)', mode=p.mode, level='ERROR', exchange=p.exchange)
        return
    if closed_ok:
        p.status = 'closed'
        p.closed_at = datetime.utcnow()
        p.last_close_error = ''
        log_event(db, f'Closed naked leg on {p.perp_symbol}: {reason}; flattened {surviving_leg or "no surviving leg"}', mode=p.mode, level='ERROR', exchange=p.exchange)


def manual_close(db, gateway: VenueGateway, p: Position, cfg: StrategyConfig) -> None:
    _force_close_both(db, gateway, p, cfg, 'manual_close')


def reconcile_positions(gateway: VenueGateway) -> None:
    """Rehydrate any perp positions on this gateway's venue that aren't tracked locally."""
    with SessionLocal() as db:
        for p in gateway.open_perp_positions_raw():
            symbol = p.get('symbol')
            pos_amt = float(p.get('contracts') or p.get('info', {}).get('positionAmt') or 0)
            existing = db.scalar(select(Position).where(Position.perp_symbol == symbol, Position.status == 'open', Position.mode == MODE_LIVE, Position.exchange == gateway.venue_id))
            if existing or pos_amt == 0:
                continue
            base = symbol.split('/')[0]
            db.add(Position(mode=MODE_LIVE, exchange=gateway.venue_id, trade_type=venue_to_trade_type(gateway.venue_id), symbol=base, spot_symbol=f'{base}/USDT', perp_symbol=symbol, quantity=abs(pos_amt), entry_funding_rate=0.0))
            log_event(db, f'Rehydrated orphan position {symbol} on {gateway.name} qty={pos_amt}', mode=MODE_LIVE, exchange=gateway.venue_id)
        db.commit()


def _usdc_usdt_rate(gateway: VenueGateway) -> float:
    """Live USDC/USDT mid for headline equity sums. Falls back to 1.0
    when the ticker isn't reachable. Cached via ``safe_price`` (30s TTL)
    so per-cycle reads don't burn rate-limit. The bot does NOT use this
    rate to decide sizing or wallet routing — those stay strictly
    per-quote so a USDC depeg can't bleed into the USDT book."""
    try:
        px = gateway.safe_price('USDC/USDT')
        if px and 0.5 < float(px) < 2.0:
            return float(px)
    except Exception:
        pass
    return 1.0


def _compute_equity_and_free(db, gateway: VenueGateway, mode: str, cfg: StrategyConfig) -> tuple[float, dict[str, float]]:
    """Returns ``(total_equity_usdt, free_by_quote)``.

    * ``total_equity_usdt`` is the venue's headline equity in USDT,
      with USDC priced at the live USDC/USDT mid (1.0 fallback).
    * ``free_by_quote`` is ``{'USDT': free_usdt, 'USDC': free_usdc}`` —
      what the bot can deploy on a fresh entry per quote currency.
      The bot never cross-funds: a USDC entry only consumes USDC.

    Per-venue scoping: every query filters by ``Position.exchange == gateway.venue_id``
    so two venues sharing the same StrategyConfig can size positions correctly
    against their own capital. Cross-venue capital movement is Phase 2.
    """
    if mode == MODE_PAPER:
        # Paper has a single virtual USDT-denominated pot. USDC trades
        # in paper draw from the same pot at 1:1 — paper is for
        # strategy validation, not depeg simulation.
        realized = total_realized_pnl(db, mode=mode, exchange=gateway.venue_id)
        open_ps = db.scalars(select(Position).where(
            Position.status == 'open',
            Position.mode == mode,
            Position.exchange == gateway.venue_id,
        )).all()
        unrealized = 0.0
        open_notional = 0.0
        open_funding = 0.0
        for p in open_ps:
            spot_now = gateway.safe_price(p.spot_symbol) or p.spot_entry_price or 0
            perp_now = gateway.safe_price(p.perp_symbol, perp=True) or p.perp_entry_price or 0
            unrealized += position_unrealized_pnl(p, spot_now, perp_now)
            open_notional += p.quantity * p.spot_entry_price
            open_funding += p.funding_income_accrued
        closed_funding = total_funding_income(db, mode=mode, status='closed', exchange=gateway.venue_id)
        capital_in = db.scalar(select(func.coalesce(func.sum(CapitalFlow.amount_usdt), 0.0)).where(
            CapitalFlow.mode == mode,
            CapitalFlow.exchange == gateway.venue_id,
        )) or 0.0
        total_equity = (cfg.paper_starting_equity + capital_in + realized + unrealized
                        + open_funding + closed_funding)
        free = max(0.0, total_equity - open_notional - unrealized)
        return total_equity, {'USDT': free, 'USDC': free}
    bals = gateway.safe_balances()
    if bals is None:
        return 0.0, {'USDT': 0.0, 'USDC': 0.0}
    rate = _usdc_usdt_rate(gateway)
    # Per-quote totals on the venue's wallets. PM / UTA gateways flatten
    # the unified pool into spot.<quote>.total; Classic gateways have
    # both spot.<quote>.total and futures.<quote>.total populated.
    quote_total_usdt = 0.0
    free_by_quote: dict[str, float] = {}
    for q in ('USDT', 'USDC'):
        spot_total = float((bals.get('spot', {}).get(q) or {}).get('total') or 0)
        fut_total = float((bals.get('futures', {}).get(q) or {}).get('total') or 0)
        spot_free = float((bals.get('spot', {}).get(q) or {}).get('free') or 0)
        fut_free = float((bals.get('futures', {}).get(q) or {}).get('free') or 0)
        amount = spot_total + fut_total  # under unified margin fut_total is 0 by convention
        quote_total_usdt += amount * (rate if q == 'USDC' else 1.0)
        # Per-leg margin still needs both wallets funded. Take min so
        # sizing accounts for the binding leg.
        free_by_quote[q] = min(spot_free, fut_free)
    # Non-stablecoin spot assets at USDT-equivalent (long leg of an
    # open arb that's not yet closed).
    spot_assets_value = 0.0
    spot_balances = bals.get('spot', {}) or {}
    META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
    for asset, bal in spot_balances.items():
        if asset in META_KEYS or asset in ('USDT', 'USDC') or not isinstance(bal, dict):
            continue
        qty = float(bal.get('total') or 0)
        if qty <= 0:
            continue
        px = gateway.safe_price(f'{asset}/USDT') or 0
        spot_assets_value += qty * px
    total_equity = quote_total_usdt + spot_assets_value
    return total_equity, free_by_quote


# Per-(mode, venue) timestamp of the last capital-flow ingest. The bot
# loop calls _maybe_ingest_capital_flows every cycle but the actual SAPI
# walk only fires once per hour — capital-flow rows don't change between
# cycles unless the user moves money, and the chunked walk eats several
# rate-limited requests. Manual triggers (the dashboard's "Re-ingest"
# button) bypass the throttle.
_CAPITAL_INGEST_INTERVAL_S = 3600.0
_LAST_CAPITAL_INGEST_AT: dict[tuple[str, str], float] = {}


# Rejected-candidate retention. The /logs search uses `RejectedCandidate`
# rows to debug why a symbol never opened — search needs to span enough
# history to be useful, but keeping the table forever bloats the DB
# (each cycle adds up to 20 rejection rows per venue per mode). 7 days
# is the ceiling; 1 day is the contractual floor.
_REJECTED_RETENTION_DAYS = 7
_REJECTED_PRUNE_INTERVAL_S = 3600.0
_LAST_REJECTED_PRUNE_AT: list[float] = [0.0]


def _maybe_prune_rejected_candidates(db) -> None:
    """Once per hour, delete rejected_candidates older than the retention
    floor. Cheap query (a single DELETE WHERE ts < cutoff)."""
    from sqlalchemy import delete
    now = time.time()
    if (now - _LAST_REJECTED_PRUNE_AT[0]) < _REJECTED_PRUNE_INTERVAL_S:
        return
    cutoff = datetime.utcnow() - timedelta(days=_REJECTED_RETENTION_DAYS)
    db.execute(delete(RejectedCandidate).where(RejectedCandidate.ts < cutoff))
    _LAST_REJECTED_PRUNE_AT[0] = now


_DUST_USDT_FLOOR = 1.0   # USDT-equivalent below which we don't bother
                         # selling (most exchanges have $5+ min notional;
                         # 1 USDT keeps the floor below the visible-clutter
                         # threshold without spamming retries on truly
                         # un-tradeable amounts)


def _sweep_non_stable_dust(db, gateway: VenueGateway, cfg: StrategyConfig) -> None:
    """Sell every non-USDT/USDC asset on the venue that isn't part of
    an open position back into USDT. Live mode only. Best-effort:
    failures log a single WARN per asset and the next cycle retries."""
    bals = gateway.safe_balances() or {}
    spot = bals.get('spot') or {}
    open_ps = db.scalars(select(Position).where(
        Position.status == 'open',
        Position.mode == MODE_LIVE,
        Position.exchange == gateway.venue_id,
    )).all()
    held_bases = {p.spot_symbol.split('/')[0] for p in open_ps}
    META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
    for asset, bal in spot.items():
        if asset in META_KEYS or not isinstance(bal, dict):
            continue
        # Stables stay; held-position bases stay; everything else gets sold.
        if asset in ('USDT', 'USDC'):
            continue
        if asset in held_bases:
            continue
        try:
            free = float(bal.get('free') or 0)
        except (TypeError, ValueError):
            continue
        if free <= 0:
            continue
        sell_symbol = f'{asset}/USDT'
        if sell_symbol not in (gateway.spot.markets or {}):
            # No /USDT pair (e.g. BFUSD, fiat, asset that only trades
            # against BTC). Skip silently — the user can move it
            # manually if they want.
            continue
        # Skip below LOT_SIZE.minQty (Binance -1013 / KuCoin equivalent)
        min_amt = gateway.market_min_amount(sell_symbol, perp=False)
        if min_amt > 0 and free < min_amt:
            continue
        px = gateway.safe_price(sell_symbol) or 0
        if px <= 0:
            continue
        notional = free * px
        # Below the heuristic visible-dust floor — not worth the API call.
        if notional < _DUST_USDT_FLOOR:
            continue
        # Skip below the venue's MIN_NOTIONAL filter (Binance -1013
        # "Filter failure: NOTIONAL"). ccxt exposes this as
        # market.limits.cost.min — usually $5-10 USDT on Binance,
        # $1 on KuCoin. Honouring it upfront avoids a guaranteed
        # rejection + the WARN log noise the user pointed out.
        try:
            mkt = gateway.spot.market(sell_symbol)
            min_cost = float(((mkt.get('limits') or {}).get('cost') or {}).get('min') or 0)
        except Exception:
            min_cost = 0.0
        if min_cost > 0 and notional < min_cost:
            continue
        try:
            gateway.close_spot(sell_symbol, free, False, cfg.paper_slippage_bps, cfg.paper_fee_bps)
            log_event(db, f'Dust sweep: sold {free:.6f} {asset} → USDT (~{notional:.2f} USDT)',
                      mode=MODE_LIVE, exchange=gateway.venue_id)
        except Exception as e:
            err = str(e)[:140]
            # MIN_NOTIONAL or LOT_SIZE rejections that slipped past our
            # pre-checks (e.g. live ticker drifted between the check and
            # the order) are uninteresting noise — skip the WARN log.
            if 'NOTIONAL' in err or 'LOT_SIZE' in err or '-1013' in err:
                continue
            log_event(db, f'Dust sweep failed for {asset} on {gateway.name}: {err}',
                      mode=MODE_LIVE, level='WARN', exchange=gateway.venue_id)


def _maybe_ingest_capital_flows(db, gateway: VenueGateway, mode: str) -> int:
    key = (mode, gateway.venue_id)
    now = time.time()
    last = _LAST_CAPITAL_INGEST_AT.get(key, 0.0)
    if (now - last) < _CAPITAL_INGEST_INTERVAL_S:
        return 0
    inserted = _ingest_api_capital_flows(db, gateway, mode)
    _LAST_CAPITAL_INGEST_AT[key] = now
    return inserted


def _ingest_api_capital_flows(db, gateway: VenueGateway, mode: str, lookback_days: int = 30) -> int:
    """Pull the venue's deposit / withdrawal / sub-transfer history and
    persist any rows we haven't seen before as ``CapitalFlow`` records. The
    natural key is ``(exchange, external_id)``; rows with that pair already
    in the DB are skipped, so this is safe to run every cycle. Returns the
    number of new rows inserted.

    LIVE mode only. Paper mode treats CapitalFlow as virtual. We log the
    by-kind row count every cycle (including zero) so the operator can see
    on /logs whether each API is returning anything — silent zero would be
    indistinguishable from "endpoint not wired" without this signal."""
    if mode != MODE_LIVE:
        return 0
    try:
        rows = gateway.list_capital_flow_records(lookback_days=lookback_days)
    except Exception as e:
        log_event(db, f'capital-flow ingest failed: {e}', mode=mode, level='WARN', exchange=gateway.venue_id)
        return 0
    counts: dict[str, int] = {}
    for r in rows:
        kind = r.get('kind') or 'deposit'
        counts[kind] = counts.get(kind, 0) + 1
    seen_ids = {x for (x,) in db.execute(select(CapitalFlow.external_id).where(
        CapitalFlow.exchange == gateway.venue_id,
        CapitalFlow.external_id != '',
    )).all()}
    inserted = 0
    for r in rows:
        ext = r.get('external_id') or ''
        if not ext or ext in seen_ids:
            continue
        db.add(CapitalFlow(
            mode=mode,
            exchange=gateway.venue_id,
            ts=r['ts'],
            amount_usdt=r['amount'],
            kind=r.get('kind') or 'deposit',
            detected_by='auto',
            note=r.get('note') or '',
            external_id=ext,
        ))
        seen_ids.add(ext)
        inserted += 1
    if inserted:
        db.flush()
    summary = ', '.join(f'{k}={v}' for k, v in counts.items()) or 'no rows returned by any endpoint'
    log_event(db, f'capital-flow ingest: {summary} · {inserted} new', mode=mode, exchange=gateway.venue_id)
    return inserted


def _take_balance_snapshot(db, gateway: VenueGateway, mode: str, cfg: StrategyConfig) -> BalanceSnapshot:
    # Both modes share the live/paper-aware equity calc which already includes
    # spot asset values for tracked positions in live mode.
    total_equity, _ = _compute_equity_and_free(db, gateway, mode, cfg)
    if mode == MODE_LIVE:
        bals = gateway.safe_balances()
        spot = float((bals['spot'].get('USDT') or {}).get('total') or 0) if bals else 0.0
        fut = float((bals['futures'].get('USDT') or {}).get('total') or 0) if bals else 0.0
        snap = BalanceSnapshot(spot_usdt=spot, futures_usdt=fut, total_usdt=total_equity, source=MODE_LIVE, exchange=gateway.venue_id)
    else:
        snap = BalanceSnapshot(spot_usdt=total_equity, futures_usdt=0.0, total_usdt=total_equity, source=mode, exchange=gateway.venue_id)
    db.add(snap)
    db.flush()
    return snap


def run_one_cycle_for_mode(gateway: VenueGateway, mode: str) -> None:
    paper = (mode == MODE_PAPER)
    try:
        with SessionLocal() as db:
            # Skip the venue entirely if every strategy that touches it
            # is disabled. Saves API calls (rate-limit budget) and log
            # noise. Re-checked every cycle so flipping a strategy on
            # via /config picks up on the next iteration without restart.
            if not venue_is_active(db, gateway.venue_id):
                return
            cfg = get_strategy_config(db)
            mstate = get_mode_state(db, mode)

            # Paper-mode funding income accrual on open positions (live is auto-credited
            # by Binance into the futures wallet, which our equity calc already sees).
            _accrue_paper_funding(db, gateway, mode)

            # Live API health probe — bail out cleanly if Binance is rejecting our auth.
            # Without this, every unwrapped order call below hits -2015 and spams the log
            # on every loop. We log once on transition to unhealthy and once on recovery.
            global _LIVE_API_UNHEALTHY_LOGGED
            if mode == MODE_LIVE:
                bals_probe = gateway.safe_balances()
                if bals_probe is None:
                    if not _LIVE_API_UNHEALTHY_LOGGED:
                        err = gateway.last_balance_error or 'unknown'
                        log_event(
                            db,
                            f'Live API unreachable: {err}. Pausing live cycles. '
                            f'Common -2015 causes: bad key/secret, IP not whitelisted on the key, '
                            f'or missing Spot/Futures permission.',
                            mode=MODE_LIVE,
                            level='ERROR',
                        )
                        _LIVE_API_UNHEALTHY_LOGGED = True
                    db.commit()
                    return
                elif _LIVE_API_UNHEALTHY_LOGGED:
                    log_event(db, f'{gateway.name} API recovered.', mode=MODE_LIVE, level='INFO', exchange=gateway.venue_id)
                    _LIVE_API_UNHEALTHY_LOGGED = False

            # Live: rehydrate any perp positions Binance shows but our DB doesn't.
            # Cheap (one auth call), keeps the dashboard in sync if the user opened
            # something outside the bot or if DB rows got out of step.
            if mode == MODE_LIVE:
                try:
                    for raw in gateway.open_perp_positions_raw():
                        sym = raw.get('symbol')
                        pos_amt = abs(float(raw.get('contracts') or raw.get('info', {}).get('positionAmt') or 0))
                        if not sym or pos_amt == 0:
                            continue
                        existing = db.scalar(select(Position).where(Position.perp_symbol == sym, Position.status == 'open', Position.mode == MODE_LIVE, Position.exchange == gateway.venue_id))
                        if existing:
                            continue
                        base = sym.split('/')[0]
                        db.add(Position(
                            mode=MODE_LIVE, exchange=gateway.venue_id, symbol=base,
                            spot_symbol=f'{base}/USDT', perp_symbol=sym,
                            quantity=pos_amt, entry_funding_rate=0.0,
                            last_funding_accrual_ts=datetime.utcnow(),
                        ))
                        log_event(db, f'Rehydrated orphan position {sym} on {gateway.name} qty={pos_amt}', mode=MODE_LIVE, level='WARN', exchange=gateway.venue_id)
                except Exception as e:
                    log_event(db, f'Reconcile failed on {gateway.name}: {str(e)[:120]}', mode=MODE_LIVE, level='WARN', exchange=gateway.venue_id)
                # And the inverse direction: if a position is open in our DB but
                # both legs are actually flat on Binance (user closed manually,
                # or a partial close fully completed outside the bot), mark it
                # closed so the UI stops showing phantom "open" rows.
                try:
                    _reconcile_open_position_state(db, gateway, mode)
                except Exception as e:
                    log_event(db, f'State reconcile failed on {gateway.name}: {str(e)[:120]}', mode=MODE_LIVE, level='WARN', exchange=gateway.venue_id)

            # Live short-circuit: if entries are off, no positions, and no
            # auto-transfer to do, skip the API hits entirely.
            if (mode == MODE_LIVE and not mstate.entry_enabled and not mstate.maintenance_mode
                    and not cfg.auto_transfer_enabled):
                open_count = db.scalar(select(func.count(Position.id)).where(Position.status == 'open', Position.mode == mode, Position.exchange == gateway.venue_id)) or 0
                if open_count == 0:
                    return

            # Maintenance — close everything in this mode and disable entries.
            if mstate.maintenance_mode:
                for p in db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode, Position.exchange == gateway.venue_id)).all():
                    _force_close_both(db, gateway, p, cfg, 'maintenance')
                if mstate.entry_enabled:
                    mstate.entry_enabled = False
                    log_event(db, 'Maintenance mode active — entries disabled', mode=mode, exchange=gateway.venue_id)
                # leave maintenance_mode set; user clears it via the dashboard
                db.commit()
                return

            # Per-strategy "exit all & stop" — when the operator clicks
            # the button on /config for a specific trade-type, the
            # ``StrategyState.exit_all_pending`` flag is set. The next
            # cycle force-closes every open position of that trade-type,
            # then clears the flag and leaves ``entry_enabled=False`` so
            # the strategy stays quiescent until explicit resume.
            this_strategy_tt = venue_to_trade_type(gateway.venue_id)
            sstate_check = get_strategy_state(db, mode, this_strategy_tt)
            if sstate_check.exit_all_pending:
                victims = db.scalars(select(Position).where(
                    Position.status == 'open',
                    Position.mode == mode,
                    Position.exchange == gateway.venue_id,
                    Position.trade_type == this_strategy_tt,
                )).all()
                for p in victims:
                    _force_close_both(db, gateway, p, cfg, f'strategy_exit_all:{this_strategy_tt}')
                sstate_check.exit_all_pending = False
                sstate_check.entry_enabled = False
                log_event(db, f'Strategy {this_strategy_tt} exit-all completed: closed {len(victims)} position(s); entries disabled until manual resume', mode=mode, exchange=gateway.venue_id)
                db.commit()

            open_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode, Position.exchange == gateway.venue_id)).all()

            # Phase A: per-position safety — live mode only (paper has no authoritative state to verify against).
            if mode == MODE_LIVE and open_positions:
                for p in list(open_positions):
                    if cfg.delisting_check:
                        healthy, market_reason = check_market_health(gateway, p)
                        if not healthy:
                            _force_close_both(db, gateway, p, cfg, f'market_unhealthy:{market_reason}')
                            continue
                    if cfg.enforce_hedge_check:
                        hedged, hedge_reason, surviving_leg = check_hedge(gateway, p)
                        if not hedged:
                            _close_naked_leg(db, gateway, p, cfg, surviving_leg, hedge_reason)
                open_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode, Position.exchange == gateway.venue_id)).all()

            # Phase B: voluntary exits.
            if mstate.exit_enabled and open_positions:
                try:
                    # Use the gateway-level method so KuCoin's custom
                    # `predictedFundingFeeRate` path is hit (raw
                    # fetch_funding_rates either NotSupported or returns
                    # last-settled rather than predicted on KuCoin).
                    current_funding = gateway.funding_rates_dict() or {}
                except Exception as e:
                    log_event(db, f'Exit funding refresh failed: {str(e)[:160]}', mode=mode, level='WARN', exchange=gateway.venue_id)
                    current_funding = {}
                for p in open_positions:
                    row = current_funding.get(p.perp_symbol) or {}
                    fr = row.get('fundingRate')
                    if fr is not None:
                        p.last_funding_rate = float(fr)
                        p.funding_interval_hours = _interval_hours(row)
                    elif current_funding:
                        # Symbol drift between ccxt versions / venue
                        # listing changes can leave a position with a
                        # perp_symbol that no longer keys into the live
                        # rates dict. Don't crash — fall back to the
                        # entry-time funding rate — but log so the
                        # operator can see the lookup miss instead of
                        # silently using stale data forever.
                        log_event(db, f'Exit funding miss for {p.perp_symbol}: not in rates dict ({len(current_funding)} symbols returned). Using stale last_funding_rate={p.last_funding_rate:+.4%}.', mode=mode, level='WARN', exchange=gateway.venue_id)
                    current_apr = annualize_rate(p.last_funding_rate, p.funding_interval_hours or 8.0)
                    age = datetime.utcnow() - p.opened_at
                    spot_now = gateway.safe_price(p.spot_symbol)
                    perp_now = gateway.safe_price(p.perp_symbol, perp=True)
                    exit_reason = None
                    mandatory = False
                    # Forward annualized net profit (the "if I were
                    # opening this fresh now, what's the rate?" check).
                    # Same SIGN-AWARE calc as the entry gate so the
                    # entry / exit thresholds are directly comparable.
                    # Positive live basis (perp > spot) is a kicker we'd
                    # capture on entry; we model worst-case exit as the
                    # basis swinging adversely by `exit_buffer × |basis|`.
                    interval_h = p.funding_interval_hours or 8.0
                    funding_window_bps = float(p.last_funding_rate) * 10000.0
                    if spot_now and perp_now and spot_now > 0:
                        live_basis_bps = (perp_now - spot_now) / spot_now * 10000.0
                    else:
                        live_basis_bps = 0.0
                    exit_buffer = float(cfg.exit_basis_buffer_multiple or 3.0)
                    # Conservative round-trip basis: long-spot/short-perp
                    # is hurt when basis goes MORE POSITIVE (perp gets
                    # expensive vs spot). Worst-case exit = current +
                    # buffer × |current|. RT = current − worst = −buffer ×
                    # |current| regardless of current's sign — same
                    # formula as the entry gate so the two thresholds are
                    # directly comparable.
                    worst_adverse_swing_bps = abs(live_basis_bps) * exit_buffer
                    rt_basis_signed_bps = -worst_adverse_swing_bps
                    spot_fee_bps = gateway.taker_fee_bps(p.spot_symbol, perp=False)
                    perp_fee_bps = gateway.taker_fee_bps(p.perp_symbol, perp=True)
                    rt_fees_bps = 2.0 * (spot_fee_bps + perp_fee_bps)
                    fwd_net_per_window_bps = funding_window_bps + rt_basis_signed_bps - rt_fees_bps
                    periods_per_year = 24.0 * 365.0 / interval_h
                    try:
                        fwd_net_apy = (1.0 + fwd_net_per_window_bps / 10000.0) ** periods_per_year - 1.0
                    except OverflowError:
                        fwd_net_apy = float('inf') if fwd_net_per_window_bps > 0 else -1.0
                    if fwd_net_apy < cfg.exit_funding_threshold:
                        exit_reason = (f'forward_profit_below_threshold '
                                       f'(net {fwd_net_apy*100:+.2f}% APY < {cfg.exit_funding_threshold*100:.2f}% required, '
                                       f'live: funding {funding_window_bps:+.1f}bps − basis {rt_basis_bps:.1f}bps − fees {rt_fees_bps:.1f}bps)')
                    elif age > timedelta(hours=cfg.max_hold_hours):
                        exit_reason = 'max_hold'
                    if p.spot_entry_price > 0 and p.perp_entry_price > 0 and spot_now and perp_now:
                        pnl = (spot_now - p.spot_entry_price) * p.quantity + (p.perp_entry_price - perp_now) * p.quantity
                        notional = p.spot_entry_price * p.quantity
                        if notional > 0 and pnl / notional <= cfg.stop_loss_pct:
                            exit_reason = 'stop_loss'
                            mandatory = True
                    if exit_reason and not mandatory and spot_now and perp_now:
                        favorable, bps = is_basis_exit_favorable(spot_now, perp_now, cfg.max_exit_basis_bps)
                        if not favorable:
                            log_event(db, f'Exit deferred for {p.perp_symbol} ({exit_reason}): basis={bps:+.1f}bps > max_exit={cfg.max_exit_basis_bps:.1f}bps', mode=mode, exchange=gateway.venue_id)
                            continue
                    if exit_reason:
                        _force_close_both(db, gateway, p, cfg, exit_reason)

            open_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode, Position.exchange == gateway.venue_id)).all()
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            daily_trades = db.scalar(select(func.count(Trade.id)).where(Trade.ts >= today_start, Trade.mode == mode)) or 0

            scan_action = 'no_scan'
            scan_note = ''
            top_candidates_payload: list[dict] = []
            candidates_total = 0
            candidates_passing = 0

            if mstate.entry_enabled and len(open_positions) < cfg.max_open_positions and daily_trades < cfg.max_trades_per_day:
                try:
                    passing, candidates_total, rejected_scan = gateway.scan_funding(
                        cfg.entry_funding_threshold,
                    )
                except Exception as e:
                    passing, candidates_total, rejected_scan = [], 0, []
                    log_event(db, f'scan_funding failed: {e}', mode=mode, level='ERROR', exchange=gateway.venue_id)
                candidates_passing = len(passing)
                # Per-quote scan coverage so /logs scan-history rows
                # show "USDT: 320 total / 12 passing · USDC: 18 total /
                # 1 passing" instead of just one aggregate count. Lets
                # the operator confirm USDC perps are reaching the
                # scanner without hunting through rejected_candidates.
                pq = getattr(gateway, 'last_scan_per_quote', {}) or {}
                if pq:
                    parts = [
                        f"{q}: {pq.get(q, {}).get('total', 0)} total / {pq.get(q, {}).get('passing', 0)} passing"
                        for q in ('USDT', 'USDC') if pq.get(q, {}).get('total', 0) > 0
                    ]
                    if parts:
                        scan_note = ' · '.join(parts)
                    # Also log to bot_events so the operator can confirm
                    # per-venue scan coverage by searching /logs for the
                    # venue name (e.g. "kucoin"). The scan_results table
                    # has the same data but isn't searchable from the
                    # current /logs UI.
                    summary_bits = []
                    for q in ('USDT', 'USDC'):
                        s = pq.get(q, {}) or {}
                        if s.get('total', 0):
                            summary_bits.append(f"{q}: {s['total']}t/{s.get('passing', 0)}p/{s.get('below_threshold', 0)}<thr")
                    if summary_bits:
                        log_event(db, f'Scan summary: {" · ".join(summary_bits)}', mode=mode, exchange=gateway.venue_id)
                top_candidates_payload = [{
                    'perp': c.perp_symbol,
                    'fr': c.funding_rate,
                    'apr': c.funding_apr,
                    'interval_h': c.funding_interval_hours,
                    'qv': c.quote_volume,
                    'spot_depth': c.spot_depth_usdt,
                    'perp_depth': c.perp_depth_usdt,
                } for c in passing[:5]]
                # Per-candidate diagnostic showing the EXACT inputs to the
                # APY calc — venue's predicted next-window rate (which may
                # differ from the rate you see in their UI; predicted is
                # forward-looking and updates continuously) plus the funding
                # interval the bot parsed from the contract metadata. If
                # the venue UI shows X% per Yh but this log shows different
                # values, it's a parser issue worth flagging.
                for c in passing[:3]:
                    log_event(
                        db,
                        f'Scan top {c.perp_symbol}: predicted rate={c.funding_rate*100:+.4f}% per {c.funding_interval_hours:.0f}h → APY={c.funding_apr*100:+.2f}%',
                        mode=mode,
                        exchange=gateway.venue_id,
                    )
                # Per-(quote, reason-category) bucket sampling. A flat
                # `rejected_scan[:60]` cap drowned out USDC rejections
                # whenever 100+ above-threshold USDT pairs hit upstream
                # filters (no_spot_market, volume<, depth<) on the same
                # scan — they're appended during the main scan loop and
                # filled the cap before the after-loop top-below USDC
                # rejections reached it. The bucket cap of 8 per
                # (quote, category) guarantees every reason × quote
                # combo gets representation in /logs.
                PER_BUCKET_CAP = 8
                PER_SCAN_CAP = 120
                _buckets: dict = {}
                _saved = 0
                for sym, reason, apr in rejected_scan:
                    quote = 'USDC' if 'USDC' in sym else ('USDT' if 'USDT' in sym else 'OTHER')
                    cat_full = reason.split(' (')[0]
                    cat = cat_full.split('<')[0].strip()[:32]  # strip "<N" tail
                    key = (quote, cat)
                    if _buckets.get(key, 0) >= PER_BUCKET_CAP:
                        continue
                    _buckets[key] = _buckets.get(key, 0) + 1
                    db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=sym, reason=reason, funding_rate=apr))
                    _saved += 1
                    if _saved >= PER_SCAN_CAP:
                        break

                # Sweep funding/main into the trade wallet BEFORE we read
                # balances for sizing. Venues like KuCoin Classic split spot
                # cash across `trade` (where orders execute) and `main`
                # (default deposit zone) — our synthesised
                # `spot.<asset>.free` aggregates both, but a market spot
                # order only sees `trade`. Without this consolidation,
                # sizing thinks we have $9.90 free, picks a $5 notional,
                # and the order fails because trade actually only has
                # $0.10. Other venues (Binance Classic / PM, KuCoin UTA)
                # have a single spot wallet — their override is a no-op.
                if mode == MODE_LIVE:
                    try:
                        moves = gateway.consolidate_spot_wallets(paper_mode=False)
                        for asset, amt, err in moves:
                            if err:
                                log_event(db, f'Spot wallet consolidate {asset}: {err[:160]}', mode=mode, level='WARN', exchange=gateway.venue_id)
                            elif amt > 0:
                                log_event(db, f'Spot wallet consolidate {asset}: {amt:.4f} main→trade (align spot.free with what spot orders can spend)', mode=mode, exchange=gateway.venue_id)
                        if any(amt > 0 for _, amt, _ in moves):
                            gateway.safe_balances(force_refresh=True)  # invalidate cache so post-sweep state is read
                    except Exception as e:
                        log_event(db, f'Spot wallet consolidate raised: {str(e)[:160]}', mode=mode, level='WARN', exchange=gateway.venue_id)

                total_equity, free_by_quote = _compute_equity_and_free(db, gateway, mode, cfg)
                # Per-quote spot/futures free for sizing. Each candidate's
                # quote_currency selects the right pool — never cross-funded.
                spot_free_by_q: dict[str, float] = {}
                fut_free_by_q: dict[str, float] = {}
                if mode == MODE_LIVE:
                    bals = gateway.safe_balances() or {}
                    for q in ('USDT', 'USDC'):
                        spot_free_by_q[q] = float((bals.get('spot', {}).get(q) or {}).get('free') or 0)
                        fut_free_by_q[q] = float((bals.get('futures', {}).get(q) or {}).get('free') or 0)
                    # Diagnostic snapshot — one line per (venue, quote)
                    # per cycle so the operator can see exactly what
                    # the balance reader returned. Includes the raw
                    # spot+futures breakdown plus fut.total which is
                    # the unified-margin marker (PM/UTA → 0; Classic
                    # → real wallet total). Searchable in /logs by
                    # "Wallet snapshot" or by venue tag.
                    is_uta_str = ''
                    if hasattr(gateway, '_is_uta'):
                        is_uta_str = ' [UTA]' if gateway._is_uta else ' [Classic]'
                    for q in ('USDT', 'USDC'):
                        sf = spot_free_by_q[q]
                        ff = fut_free_by_q[q]
                        st = float((bals.get('spot', {}).get(q) or {}).get('total') or 0)
                        ft = float((bals.get('futures', {}).get(q) or {}).get('total') or 0)
                        if st > 0 or ft > 0 or sf > 0 or ff > 0:
                            unified = '·unified' if ft <= 0.001 else '·split'
                            log_event(db, f'Wallet snapshot {q}{is_uta_str}{unified}: spot free/total={sf:.4f}/{st:.4f}; fut free/total={ff:.4f}/{ft:.4f}', mode=mode, exchange=gateway.venue_id)
                else:
                    # Paper has a single pool; both quotes draw from the
                    # same paper equity at 1:1 (paper is for strategy
                    # validation, not depeg simulation).
                    for q in ('USDT', 'USDC'):
                        spot_free_by_q[q] = fut_free_by_q[q] = free_by_quote.get(q, 0.0)

                if total_equity <= 0:
                    scan_action = 'balances_unavailable'
                    scan_note = 'safe_balances() returned None or paper equity = 0'
                else:
                    scan_action = 'no_fill'
                    desired_notional = cfg.max_position_pct * total_equity
                    min_notional = cfg.min_position_pct * total_equity

                    # Pre-trade wallet rebalance (Classic accounts only).
                    # KuCoin Classic and Binance Classic split USDT
                    # across separate spot + contract wallets — if all
                    # the cash sits in one bucket, the OTHER leg is
                    # under-funded and our `min(spot_free, fut_free)`
                    # sizing reads the small side. Equalize each
                    # cycle so both legs can fund their share. Under
                    # unified margin (Binance PM, KuCoin UTA) the
                    # synthesised futures.<asset>.total is 0 by
                    # convention — we skip in that case since the
                    # buckets aren't actually separate wallets.
                    if mode == MODE_LIVE and not cfg.auto_transfer_enabled:
                        # Operator has explicitly disabled wallet rebalancing
                        # on /config. With this off the bot will under-size
                        # to whichever leg is smallest. Surface it once per
                        # cycle so the symptom isn't silent.
                        log_event(db, 'Pre-trade rebalance skipped: auto_transfer_enabled=False on /config (under-sizing to smaller leg)', mode=mode, level='WARN', exchange=gateway.venue_id)
                    if mode == MODE_LIVE and cfg.auto_transfer_enabled and gateway.is_unified_margin():
                        # Authoritative unified-margin check — replaces the old
                        # `fut.total <= 0.001` heuristic which mis-classified an
                        # EMPTY contract wallet as unified, silently skipping
                        # the rebalance on a KuCoin Classic account where all
                        # the cash sat on the spot side.
                        log_event(db, f'Pre-trade rebalance skipped: {gateway.name} reports unified margin (no spot↔futures transfer meaningful)', mode=mode, exchange=gateway.venue_id)
                    elif mode == MODE_LIVE and cfg.auto_transfer_enabled:
                        for q in ('USDT', 'USDC'):
                            sf = spot_free_by_q.get(q, 0.0)
                            ff = fut_free_by_q.get(q, 0.0)
                            imbalance = abs(sf - ff)
                            # Don't bother for tiny gaps (transfer fees +
                            # noise dominate). 20 cents threshold.
                            if imbalance < 0.20:
                                continue
                            midpoint = (sf + ff) / 2.0
                            if sf < midpoint:
                                move = midpoint - sf
                                ok, err = gateway.transfer_futures_to_spot(move, paper, asset=q)
                                if ok:
                                    spot_free_by_q[q] = sf + move
                                    fut_free_by_q[q] = ff - move
                                    log_event(db, f'Pre-trade rebalance: {move:.2f} {q} futures→spot (equalize wallets so both legs can fund)', mode=mode, exchange=gateway.venue_id)
                                else:
                                    log_event(db, f'Pre-trade futures→spot {q} rebalance failed: {err[:200]}', mode=mode, level='WARN', exchange=gateway.venue_id)
                            else:
                                move = sf - midpoint
                                ok, err = gateway.transfer_spot_to_futures(move, paper, asset=q)
                                if ok:
                                    spot_free_by_q[q] = sf - move
                                    fut_free_by_q[q] = ff + move
                                    log_event(db, f'Pre-trade rebalance: {move:.2f} {q} spot→futures (equalize wallets so both legs can fund)', mode=mode, exchange=gateway.venue_id)
                                else:
                                    log_event(db, f'Pre-trade spot→futures {q} rebalance failed: {err[:200]}', mode=mode, level='WARN', exchange=gateway.venue_id)

                    # Diversity: skip any candidate whose base asset we already hold open
                    # in this mode. Avoids piling more capital into the same name even
                    # though the perp symbol formally matches; a clean default for the
                    # max_open_positions > 1 case.
                    held_bases = {p.spot_symbol.split('/')[0] for p in db.scalars(
                        select(Position).where(Position.status == 'open', Position.mode == mode, Position.exchange == gateway.venue_id)
                    ).all()}
                    # Per-strategy gate. The candidate inherits the
                    # gateway's trade-type tag (same-venue funding arb
                    # today; cross-venue / onchain trade types route
                    # through different orchestrators when wired). If
                    # the operator has flipped this strategy off on
                    # /config, skip the whole open path — no candidates
                    # of this type get filled this cycle.
                    candidate_trade_type = venue_to_trade_type(gateway.venue_id)
                    sstate = get_strategy_state(db, mode, candidate_trade_type)
                    if not sstate.entry_enabled:
                        scan_action = f'strategy_disabled:{candidate_trade_type}'
                        passing = []
                    for c in passing[:5]:
                        base = c.spot_symbol.split('/')[0]
                        if base in held_bases:
                            continue
                        # Per-quote sizing. Spot leg consumes the spot
                        # quote's free balance; perp leg consumes the
                        # perp quote's free balance. For same-stable arbs
                        # both are equal (USDT or USDC); for cross-stable
                        # arbs (e.g. DOGE/USDT spot + DOGEUSDC perp) they
                        # differ. The wallet cap is the smaller of the
                        # two so we never open a leg we can't fund.
                        cq = c.quote_currency
                        sq = getattr(c, 'spot_quote_currency', cq)
                        spot_leg_free = spot_free_by_q.get(sq, 0.0)
                        perp_leg_free = fut_free_by_q.get(cq, 0.0)
                        free_for_arb = min(spot_leg_free, perp_leg_free)
                        wallet_cap = free_for_arb * 0.97
                        sized_notional = min(wallet_cap, desired_notional)
                        # Auto-swap USDT ↔ USDC when one quote's wallet
                        # is empty but the OTHER stable has surplus —
                        # lets a USDT-funded account take USDC-quoted
                        # perp opportunities (and vice versa) without
                        # the operator manually pre-allocating both
                        # stables. Goes through the gateway's protected
                        # swap_quote which has a ±50 bps de-peg guard.
                        if (sized_notional < min_notional
                                and mode == MODE_LIVE
                                and getattr(cfg, 'auto_quote_swap_enabled', True)
                                and sq == cq):  # only attempt when both legs need the same quote
                            short_q = cq
                            surplus_q = 'USDT' if short_q == 'USDC' else 'USDC'
                            surplus_total_free = spot_free_by_q.get(surplus_q, 0.0) + fut_free_by_q.get(surplus_q, 0.0)
                            target_swap = min_notional * 1.5  # buffer over the floor
                            # Don't drain more than half the surplus quote — leaves
                            # room for further trades on the other stable.
                            if surplus_total_free >= target_swap * 1.05 and surplus_total_free > 1.0:
                                cap_by_surplus = surplus_total_free * 0.5
                                target_swap = min(target_swap, cap_by_surplus)
                                filled, swap_avg, swap_err = gateway.swap_quote(
                                    surplus_q, short_q, target_swap, paper, slippage_bps=cfg.paper_slippage_bps,
                                )
                                if filled > 0:
                                    log_event(db, f'Auto-swap {surplus_q}→{short_q}: acquired {filled:.4f} {short_q} @ {swap_avg:.4f} for {c.perp_symbol} entry', mode=mode, exchange=gateway.venue_id)
                                    # Re-consolidate spot wallets after the
                                    # swap. KuCoin's swap_quote executes on
                                    # the spot order book, which lands the
                                    # filled quote in the `trade` wallet —
                                    # already where we want it for the
                                    # subsequent buy. Most other venues only
                                    # have one spot wallet, so this is a
                                    # no-op for them. We call it anyway as
                                    # belt-and-suspenders against future
                                    # venues whose swap might land in
                                    # `main`/`funding`/etc.
                                    try:
                                        gateway.consolidate_spot_wallets(paper_mode=paper)
                                    except Exception:
                                        pass
                                    # Re-read live balances and equalise
                                    # the new short_q across spot/futures
                                    # wallets (Classic only — under
                                    # unified margin the swap is already
                                    # available to either leg).
                                    bals_after = gateway.safe_balances(force_refresh=True) or {}
                                    for q in ('USDT', 'USDC'):
                                        spot_free_by_q[q] = float((bals_after.get('spot', {}).get(q) or {}).get('free') or 0)
                                        fut_free_by_q[q] = float((bals_after.get('futures', {}).get(q) or {}).get('free') or 0)
                                    fut_total_q = float((bals_after.get('futures', {}).get(short_q) or {}).get('total') or 0)
                                    if fut_total_q > 0.001:
                                        # Classic — split new short_q half-and-half across wallets.
                                        sf2 = spot_free_by_q.get(short_q, 0.0)
                                        ff2 = fut_free_by_q.get(short_q, 0.0)
                                        midpoint = (sf2 + ff2) / 2.0
                                        if sf2 > midpoint + 0.10:
                                            move = sf2 - midpoint
                                            ok2, err2 = gateway.transfer_spot_to_futures(move, paper, asset=short_q)
                                            if ok2:
                                                spot_free_by_q[short_q] = sf2 - move
                                                fut_free_by_q[short_q] = ff2 + move
                                                log_event(db, f'Post-swap rebalance: {move:.4f} {short_q} spot→futures (split for arb legs)', mode=mode, exchange=gateway.venue_id)
                                    # Recompute sizing.
                                    spot_leg_free = spot_free_by_q.get(sq, 0.0)
                                    perp_leg_free = fut_free_by_q.get(cq, 0.0)
                                    free_for_arb = min(spot_leg_free, perp_leg_free)
                                    wallet_cap = free_for_arb * 0.97
                                    sized_notional = min(wallet_cap, desired_notional)
                                else:
                                    log_event(db, f'Auto-swap {surplus_q}→{short_q} skipped: {swap_err[:120]}', mode=mode, level='WARN', exchange=gateway.venue_id)
                        if sized_notional < min_notional:
                            quote_label = cq if sq == cq else f'{sq}-spot/{cq}-perp'
                            uta_tag = ''
                            if hasattr(gateway, '_is_uta'):
                                uta_tag = ' UTA' if gateway._is_uta else ' Classic'
                            reason = (f'below min position pct ({sized_notional:.4f} < {min_notional:.4f}; '
                                      f'wallet={wallet_cap:.4f} [{quote_label}{uta_tag}] '
                                      f'spot_leg_free={spot_leg_free:.4f}[{sq}] '
                                      f'perp_leg_free={perp_leg_free:.4f}[{cq}] '
                                      f'max_pct={desired_notional:.4f} '
                                      f'min_pct={min_notional:.4f})')
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol, reason=reason, funding_rate=c.funding_apr))
                            scan_action = 'below_min_pct'
                            continue
                        # Protected-execution path: walk both order books
                        # to confirm the desired qty can fill at acceptable
                        # cross-leg basis BEFORE placing any order. The
                        # limit-IOC limit price comes from the deepest
                        # level we'd consume + a 1-tick buffer, so the
                        # order either fills the whole walk or cancels —
                        # never leaves a resting limit on the book.
                        spot_px_quick = gateway.safe_price(c.spot_symbol) or 0
                        if spot_px_quick <= 0:
                            continue
                        target_qty = sized_notional / spot_px_quick
                        # Iterative shrink: each pass walks the book and
                        # shrinks for both fillable depth AND venue-side
                        # balance reservation (limit_price × qty must fit
                        # in each leg's free balance). Converges fast — at
                        # most 4 iterations because each shrink reason can
                        # only fire once cleanly. Putting the reservation
                        # clamp INSIDE the loop means the profitability
                        # gate downstream sees fill prices for the FINAL
                        # post-clamp size, not pre-clamp prices that would
                        # mis-state the actual trade economics.
                        tick_bps = float(cfg.entry_tick_buffer_bps or 1.0)
                        leverage = max(1.0, float(cfg.perp_leverage or 1))
                        spot_sim = perp_sim = None
                        for _attempt in range(4):
                            with ThreadPoolExecutor(max_workers=2) as ex:
                                spot_future = ex.submit(gateway.simulate_fill, c.spot_symbol, target_qty, side='buy', perp=False)
                                perp_future = ex.submit(gateway.simulate_fill, c.perp_symbol, target_qty, side='sell', perp=True)
                                spot_sim = spot_future.result()
                                perp_sim = perp_future.result()
                            spot_filled = spot_sim['filled_qty']
                            perp_filled = perp_sim['filled_qty']
                            min_filled = min(spot_filled, perp_filled)
                            if min_filled <= 0:
                                target_qty = 0
                                break
                            # Reservation clamp using the prices THIS walk
                            # produced. Recomputed each pass because
                            # shrinking target_qty changes worst_price.
                            spot_worst = spot_sim['worst_price']
                            perp_worst = perp_sim['worst_price']
                            spot_lim_provisional = spot_worst * (1 + tick_bps / 10000.0) if spot_worst > 0 else 0.0
                            perp_lim_provisional = perp_worst * (1 - tick_bps / 10000.0) if perp_worst > 0 else 0.0
                            max_by_spot_bal = (spot_leg_free * 0.99) / spot_lim_provisional if spot_lim_provisional > 0 else target_qty
                            max_by_perp_bal = (perp_leg_free * 0.99 * leverage) / perp_lim_provisional if perp_lim_provisional > 0 else target_qty
                            target_qty_new = min(min_filled, max_by_spot_bal, max_by_perp_bal, target_qty)
                            if target_qty_new >= target_qty - 1e-9:
                                break  # converged: depth + reservation both fit
                            target_qty = target_qty_new
                        if target_qty <= 0 or spot_sim is None:
                            # Surface WHY both legs read zero so we can tell
                            # apart "fetch_order_book threw" from "empty book"
                            # from "ccxt symbol shape mismatch / pair not
                            # listed". Without the inner errors we can't
                            # distinguish a venue-side outage from a wrong
                            # symbol shape.
                            spot_err = (spot_sim or {}).get('error', '?') if spot_sim else 'no_spot_sim'
                            perp_err = (perp_sim or {}).get('error', '?') if perp_sim else 'no_perp_sim'
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol,
                                reason=(f'no_book_depth (spot_filled={spot_sim["filled_qty"] if spot_sim else 0:.6f}, '
                                        f'perp_filled={perp_sim["filled_qty"] if perp_sim else 0:.6f}; '
                                        f'spot_err={spot_err[:80]}; perp_err={perp_err[:80]}; '
                                        f'spot_sym={c.spot_symbol}; perp_sym={c.perp_symbol})'),
                                funding_rate=c.funding_apr))
                            scan_action = 'no_book_depth'
                            continue
                        # Re-check the per-position-pct floor against the
                        # final qty (book-walk may have shrunk us below it).
                        sim_notional = target_qty * spot_sim['avg_price']
                        if sim_notional < min_notional:
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol,
                                reason=f'below_min_pct_after_book_walk ({sim_notional:.2f} < {min_notional:.2f})',
                                funding_rate=c.funding_apr))
                            scan_action = 'below_min_pct'
                            continue
                        # Synthetic basis at fill prices — the REAL entry
                        # cost vs. the mid-price approximation we used to
                        # check. (perp_avg − spot_avg) / spot_avg × 1e4.
                        spot_avg = spot_sim['avg_price']
                        perp_avg = perp_sim['avg_price']
                        if spot_avg <= 0 or perp_avg <= 0:
                            continue
                        fill_basis_bps = (perp_avg - spot_avg) / spot_avg * 10000.0
                        # No standalone basis sanity gate — the profitability
                        # gate immediately below incorporates fill_basis_bps
                        # (signed) plus the worst-case adverse-exit swing
                        # plus round-trip taker fees, then compares the
                        # annualized net profit to entry_funding_threshold.
                        # That single check subsumes the old "is this basis
                        # reasonable?" question: if the basis cost overwhelms
                        # the funding kicker the candidate fails APY, and if
                        # it doesn't the trade IS economic. Letting one gate
                        # decide rather than two avoids rejecting candidates
                        # that the profitability model would have approved.
                        # Profitability gate. Compute net expected profit per
                        # single funding window using actual API-reported
                        # taker fees, annualize (compounded), and compare to
                        # the operator's entry-annualized-net-profit
                        # threshold. The components are SIGNED — positive
                        # entry basis shows up as income, negative as cost.
                        #
                        # Round-trip basis P&L for long-spot/short-perp:
                        #   = entry_basis − exit_basis  (signed bps)
                        # We don't know exit_basis at entry, so we model
                        # worst-case adversely: assume the exit basis swings
                        # by exit_basis_buffer_multiple × |entry_basis| in
                        # the direction that hurts us. For positive entry
                        # (kicker), worst-case exit is FURTHER positive
                        # (we'd buy back the perp at an even higher premium
                        # than we sold it at). For negative entry (cost),
                        # worst-case exit is FURTHER negative.
                        funding_window_bps = c.funding_rate * 10000.0
                        exit_buffer = float(cfg.exit_basis_buffer_multiple or 3.0)
                        # Conservative round-trip basis: long-spot/short-perp
                        # is hurt when basis goes MORE POSITIVE (perp gets
                        # expensive vs spot). Worst-case exit = entry + buffer
                        # × |entry|. RT = entry − worst_exit = −buffer × |entry|
                        # for both signs of entry. Positive entry kicker is
                        # eaten by the worst-case adverse swing assumption —
                        # the gate stays conservative; if real exit basis is
                        # better, the trade outperforms the model.
                        worst_adverse_swing_bps = abs(fill_basis_bps) * exit_buffer
                        round_trip_basis_signed_bps = -worst_adverse_swing_bps
                        spot_fee_bps = gateway.taker_fee_bps(c.spot_symbol, perp=False)
                        perp_fee_bps = gateway.taker_fee_bps(c.perp_symbol, perp=True)
                        round_trip_fees_bps = 2.0 * (spot_fee_bps + perp_fee_bps)
                        net_per_window_bps = funding_window_bps + round_trip_basis_signed_bps - round_trip_fees_bps
                        # Annualize (compounded) using each candidate's
                        # own funding-interval-derived periods/year so
                        # 4h-cycle pairs (binance-perp) and 8h-cycle
                        # pairs (most kucoin) annualize correctly.
                        interval_h = c.funding_interval_hours or 8.0
                        periods_per_year = 24.0 * 365.0 / interval_h
                        try:
                            net_apy = (1.0 + net_per_window_bps / 10000.0) ** periods_per_year - 1.0
                        except OverflowError:
                            net_apy = float('inf') if net_per_window_bps > 0 else -1.0
                        if net_apy < cfg.entry_funding_threshold:
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol,
                                reason=(f'insufficient_annualized_profit '
                                        f'(funding={funding_window_bps:+.1f}bps + '
                                        f'basis_RT={round_trip_basis_signed_bps:+.1f}bps [entry {fill_basis_bps:+.1f} − worst-swing {worst_adverse_swing_bps:.1f} (×{exit_buffer:.1f})] − '
                                        f'fees={round_trip_fees_bps:.1f}bps [spot {spot_fee_bps:.1f} + perp {perp_fee_bps:.1f}, ×2 RT] '
                                        f'= net {net_per_window_bps:+.1f}bps/{int(interval_h)}h '
                                        f'→ {net_apy*100:+.2f}% APY < {cfg.entry_funding_threshold*100:.2f}% required)'),
                                funding_rate=c.funding_apr))
                            scan_action = 'insufficient_annualized_profit'
                            continue
                        # Cache the projected APY for the open log.
                        net_profit_bps = net_per_window_bps  # backwards-compat name used below
                        # Final limit prices for placement (recomputed
                        # outside the loop with the converged worst_price).
                        spot_limit = spot_sim['worst_price'] * (1 + tick_bps / 10000.0)
                        perp_limit = perp_sim['worst_price'] * (1 - tick_bps / 10000.0)

                        # Configure perp cross + leverage idempotently.
                        if not paper:
                            cfg_ok, cfg_err = gateway.configure_perp_for_arb(c.perp_symbol)
                            if not cfg_ok:
                                log_event(db, f'configure_perp_for_arb({c.perp_symbol}) warned: {cfg_err[:120]}', mode=mode, level='WARN', exchange=gateway.venue_id)

                        # Place spot limit-IOC.
                        try:
                            s = gateway.create_spot_buy_limit_ioc(c.spot_symbol, target_qty, spot_limit, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                        except Exception as e:
                            err = str(e)[:140]
                            log_event(db, f'Spot limit-IOC buy failed for {c.spot_symbol}: {err}', mode=mode, level='ERROR', exchange=gateway.venue_id)
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol, reason=f'spot_buy_error: {err[:80]}', funding_rate=c.funding_apr))
                            scan_action = 'spot_buy_error'
                            continue
                        spot_filled_qty = float(s.get('filled') or s.get('amount') or 0)
                        if spot_filled_qty <= 0:
                            log_event(db, f'Spot limit-IOC for {c.spot_symbol} got zero fill at limit {spot_limit:.6f} (book moved); skipping', mode=mode, level='WARN', exchange=gateway.venue_id)
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol, reason='spot_ioc_zero_fill (book moved during round-trip)', funding_rate=c.funding_apr))
                            scan_action = 'spot_ioc_zero_fill'
                            continue
                        # Persist half-built position so any failure path
                        # has a row to attach trades / errors to.
                        pos = Position(
                            mode=mode,
                            exchange=gateway.venue_id,
                            trade_type=venue_to_trade_type(gateway.venue_id),
                            symbol=c.spot_symbol.split('/')[0],
                            spot_symbol=c.spot_symbol,
                            perp_symbol=c.perp_symbol,
                            quote_currency=cq,
                            spot_quote_currency=sq,
                            quantity=spot_filled_qty,  # finalised after perp leg
                            entry_funding_rate=c.funding_rate,
                            last_funding_rate=c.funding_rate,
                            funding_interval_hours=c.funding_interval_hours,
                            spot_entry_price=float(s.get('price') or 0),
                            perp_entry_price=0.0,
                            last_funding_accrual_ts=datetime.utcnow(),
                        )
                        db.add(pos)
                        db.flush()
                        record_trade(db, pos.id, mode, c.spot_symbol, 'spot', 'buy', spot_filled_qty, s, exchange=gateway.venue_id)

                        # Place perp limit-IOC for the EXACT spot-filled qty.
                        try:
                            f = gateway.create_perp_short_limit_ioc(c.perp_symbol, spot_filled_qty, perp_limit, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                        except Exception as e:
                            err = str(e)[:140]
                            log_event(db, f'Perp limit-IOC short failed for {c.perp_symbol} after spot filled: {err} — rolling back spot leg', mode=mode, level='ERROR', exchange=gateway.venue_id)
                            _rollback_spot(db, gateway, pos, c, spot_filled_qty, paper, cfg)
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol, reason=f'perp_short_error: {err[:80]}', funding_rate=c.funding_apr))
                            scan_action = 'perp_short_error'
                            continue
                        perp_filled_qty = float(f.get('filled') or 0)
                        # Reconcile leg sizes.
                        if perp_filled_qty <= 0:
                            log_event(db, f'Perp limit-IOC for {c.perp_symbol} got zero fill — full spot rollback', mode=mode, level='ERROR', exchange=gateway.venue_id)
                            _rollback_spot(db, gateway, pos, c, spot_filled_qty, paper, cfg)
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol, reason='perp_ioc_zero_fill (book moved during round-trip)', funding_rate=c.funding_apr))
                            scan_action = 'perp_ioc_zero_fill'
                            continue
                        if perp_filled_qty + 1e-9 < spot_filled_qty:
                            # Trim spot back to perp-filled size.
                            trim_qty = spot_filled_qty - perp_filled_qty
                            log_event(db, f'Leg-mismatch reconcile: trimming {trim_qty:.6f} {base} (spot {spot_filled_qty:.6f} > perp {perp_filled_qty:.6f})', mode=mode, level='WARN', exchange=gateway.venue_id)
                            try:
                                trim_sim = gateway.simulate_fill(c.spot_symbol, trim_qty, side='sell', perp=False)
                                trim_limit = trim_sim['worst_price'] * (1 - tick_bps / 10000.0) if trim_sim['worst_price'] > 0 else spot_avg * 0.999
                                rev = gateway.close_spot_limit_ioc(c.spot_symbol, trim_qty, trim_limit, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                                trimmed = float(rev.get('filled') or 0)
                                if trimmed > 0:
                                    record_trade(db, pos.id, mode, c.spot_symbol, 'spot', 'sell', trimmed, rev, exchange=gateway.venue_id)
                            except Exception as e2:
                                log_event(db, f'Trim-spot reconcile failed: {str(e2)[:120]}. Hedge check will repair next cycle.', mode=mode, level='ERROR', exchange=gateway.venue_id)
                            pos.quantity = perp_filled_qty
                        # Both legs filled (possibly post-trim) — finalize.
                        pos.perp_entry_price = float(f.get('price') or 0)
                        record_trade(db, pos.id, mode, c.perp_symbol, 'futures', 'sell', perp_filled_qty, f, exchange=gateway.venue_id)
                        held_bases.add(base)
                        scan_action = f'opened {c.perp_symbol}'
                        cross_stable_tag = '' if sq == cq else f' [cross-stable {sq}-spot / {cq}-perp]'
                        log_event(db, f'Opened {c.perp_symbol} qty={pos.quantity:.6f} funding_apy={c.funding_apr:.2%} fill_basis={fill_basis_bps:+.1f}bps net_profit_per_window={net_profit_bps:+.1f}bps net_apy={net_apy*100:+.2f}% spot@{spot_avg:.6f} perp@{perp_avg:.6f}{cross_stable_tag}', mode=mode, exchange=gateway.venue_id)
                        bus.emit('position_opened',
                                 position_id=pos.id, mode=mode, exchange=gateway.venue_id,
                                 symbol=c.spot_symbol, quantity=pos.quantity,
                                 notional=pos.quantity * pos.spot_entry_price)
                        break
            else:
                if not mstate.entry_enabled:
                    scan_action = 'entry_disabled'
                elif len(open_positions) >= cfg.max_open_positions:
                    scan_action = 'max_positions_reached'
                elif daily_trades >= cfg.max_trades_per_day:
                    scan_action = 'daily_limit_reached'

            db.add(ScanResult(
                mode=mode,
                exchange=gateway.venue_id,
                candidates_total=candidates_total,
                candidates_passing=candidates_passing,
                top_candidates=json.dumps(top_candidates_payload),
                action=scan_action,
                note=scan_note,
            ))

            # End-of-cycle wallet rebalance (live, per-quote): drain
            # surplus futures balance back to spot per quote currency.
            # Buffer = ``cfg.futures_buffer_pct`` × open perp notional in
            # that quote (cross margin pools across symbols of the same
            # quote, not across quotes). With no open positions in a
            # given quote, drain to dust.
            if mode == MODE_LIVE and cfg.auto_transfer_enabled:
                open_ps = db.scalars(select(Position).where(
                    Position.status == 'open',
                    Position.mode == mode,
                    Position.exchange == gateway.venue_id,
                )).all()
                bals_after = gateway.safe_balances() or {}
                for q in ('USDT', 'USDC'):
                    fut_free_now = float((bals_after.get('futures', {}).get(q) or {}).get('free') or 0)
                    fut_total_now = float((bals_after.get('futures', {}).get(q) or {}).get('total') or 0)
                    # PM / UTA: total=0 by convention since the pool isn't a
                    # separate wallet. Skip drain on unified margin.
                    if fut_total_now <= 0.001:
                        continue
                    open_ps_q = [p for p in open_ps if (p.quote_currency or 'USDT') == q]
                    if open_ps_q:
                        open_notional = sum((p.quantity or 0) * (p.perp_entry_price or 0) for p in open_ps_q)
                        keep = max(0.20, open_notional * float(cfg.futures_buffer_pct or 0.20))
                    else:
                        keep = 0.10
                    if fut_free_now > keep + 0.10:
                        amt = fut_free_now - keep
                        ok, err = gateway.transfer_futures_to_spot(amt, paper, asset=q)
                        if ok:
                            log_event(db, f'Drained {amt:.2f} {q} futures→spot (kept {keep:.2f} as margin buffer; {len(open_ps_q)} {q} pos open)', mode=mode, exchange=gateway.venue_id)
                        else:
                            log_event(db, f'futures→spot {q} drain failed: {err}', mode=mode, level='WARN', exchange=gateway.venue_id)

            # Dust sweep — convert any non-stable, non-position asset
            # back to USDT so the wallet stays clean. Runs every cycle
            # in live mode. Skips USDT/USDC (already stable), assets
            # that are the spot leg of an open position on this venue
            # (selling those would unhedge the arb), assets without a
            # /USDT spot market, and quantities below the symbol's
            # min-lot. Logs each successful sweep at INFO; failures
            # at WARN so the operator sees a stuck dust balance.
            if mode == MODE_LIVE:
                _sweep_non_stable_dust(db, gateway, cfg)

            # End-of-cycle bookkeeping. Two cadences:
            #   * Capital-flow ingest (deposits / withdrawals / transfers)
            #     is throttled to once per hour per venue. Master ↔ sub
            #     transfers don't change every 30s, and the SAPI walk
            #     (especially when chunked into 30-day windows) eats
            #     several rate-limited calls. The "Re-ingest from venue
            #     APIs" button on /dashboard runs this on demand for the
            #     user when they expect a fresh row.
            #   * Balance snapshot + equity-curve point are continuous —
            #     these are cheap (cached safe_balances) and feed the
            #     real-time total-account-value indicator.
            _maybe_ingest_capital_flows(db, gateway, mode)
            _maybe_prune_rejected_candidates(db)
            snap = _take_balance_snapshot(db, gateway, mode, cfg)
            db.add(EquityCurve(mode=mode, exchange=gateway.venue_id, equity_usdt=snap.total_usdt))
            db.commit()
    except Exception as e:
        with SessionLocal() as db:
            log_event(db, f'Loop iteration error ({mode}): {e}', mode=mode, level='ERROR', exchange=gateway.venue_id)
            db.commit()


def run_one_cycle(gateways: list | None = None, mode: str | None = None) -> int:
    """Run one cycle across every configured venue. If `mode` is given, only
    that mode runs. Otherwise both. ``gateways`` is the ordered list returned
    by :func:`app.exchange.make_gateways` — each cycle scans, opens, and
    closes per venue independently. Cross-venue capital movement is Phase 2."""
    if gateways is None:
        gateways = make_gateways()
        for gw in gateways:
            try:
                gw.load_markets()
            except Exception as e:
                with SessionLocal() as db:
                    log_event(db, f'{gw.name} load_markets failed: {e}', mode=MODE_PAPER, level='ERROR', exchange=gw.venue_id)
                    db.commit()
    sleep_seconds = 30
    with SessionLocal() as db:
        cfg = get_strategy_config(db)
        sleep_seconds = max(5, int(cfg.loop_seconds))
    targets = (mode,) if mode else ALL_MODES
    for m in targets:
        for gw in gateways:
            run_one_cycle_for_mode(gw, m)
    return sleep_seconds


def run_loop() -> None:
    gateways = make_gateways()
    for gw in gateways:
        try:
            gw.load_markets()
        except Exception as e:
            with SessionLocal() as db:
                log_event(db, f'{gw.name} load_markets failed: {e}', mode=MODE_PAPER, level='ERROR', exchange=gw.venue_id)
                db.commit()
        # Eager-warm the per-symbol taker-fee cache via ccxt's
        # `fetch_trading_fees()` batch endpoint (one HTTP call per
        # client, vs. lazy per-symbol fetches on first miss). This
        # makes Tier 1's approx-profit pre-filter and Tier 3's
        # exact profit gate both work from cycle 1 instead of
        # warming up over the first hour. Silent failure falls
        # back to per-symbol lazy fetch on first cache miss.
        try:
            spot_n, fut_n = gw.prefetch_trading_fees()
            with SessionLocal() as db:
                log_event(db, f'{gw.name} pre-fetched taker fees: {spot_n} spot + {fut_n} futures symbols', mode=MODE_PAPER, exchange=gw.venue_id)
                db.commit()
        except Exception as e:
            with SessionLocal() as db:
                log_event(db, f'{gw.name} prefetch_trading_fees failed (will fall back to lazy): {str(e)[:120]}', mode=MODE_PAPER, level='WARN', exchange=gw.venue_id)
                db.commit()
        # Log the live account type so /logs shows whether the bot
        # is treating this venue as Classic vs unified-margin
        # (Binance PM, KuCoin UTA). If a wallet-balance-related
        # rejection turns out to be a misdetection, this is the
        # quickest place to verify what the bot actually sees.
        try:
            label, detail = gw.account_type()
            with SessionLocal() as db:
                log_event(db, f'{gw.name} account type at startup: {label} — {detail}', mode=MODE_PAPER, exchange=gw.venue_id)
                db.commit()
        except Exception as e:
            with SessionLocal() as db:
                log_event(db, f'{gw.name} account_type probe failed: {str(e)[:120]}', mode=MODE_PAPER, level='WARN', exchange=gw.venue_id)
                db.commit()
    for gw in gateways:
        reconcile_positions(gw)
    while True:
        sleep_seconds = run_one_cycle(gateways)
        time.sleep(sleep_seconds)
