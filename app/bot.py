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
  :func:`get_strategy_config`, :func:`get_earn_state`.
* Closing: :func:`_force_close_both`, :func:`_close_naked_leg`,
  :func:`_ensure_close_readiness`, :func:`_actual_spot_qty`,
  :func:`_actual_perp_qty`, :func:`manual_close`.
* Capital provisioning: pre-trade earn redemption + spot↔futures top-up,
  :func:`_take_balance_snapshot`.
* Paper-mode simulation: :func:`_accrue_paper_yield`,
  :func:`_accrue_paper_funding`.
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
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.events import bus
from app.exchange import BINANCE_ERR_NO_EARN_POSITION, VenueGateway, _interval_hours, annualize_rate, make_gateways
from app.finance import position_realized_pnl, position_unrealized_pnl, total_realized_pnl
from app.models import (
    ALL_MODES,
    MODE_LIVE,
    MODE_PAPER,
    VENUE_BINANCE,
    BalanceSnapshot,
    BotEvent,
    CapitalFlow,
    EarnState,
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


def _earn_sweep_for_venue(mode: str, exchange: str, **_ignored) -> None:
    """Event handler: try an earn-sweep on (mode, exchange) right now.
    Subscribed to ``position_closed`` and ``deposit_detected`` so freed-up
    or newly-arrived cash flows to earn without waiting for the next
    cycle. Respects the gateway's per-asset cooldown (use the dashboard
    "Sweep now" button to bypass that).

    Side-effect-only — opens its own DB session and gateway, doesn't
    return anything. Safe to call from any thread because the gateway
    instances we make here are throwaway."""
    if mode != MODE_LIVE:
        return
    gateways = make_gateways()
    target = next((g for g in gateways if g.venue_id == exchange), None)
    if target is None:
        return
    with SessionLocal() as db:
        cfg = get_strategy_config(db)
        if not cfg.earn_enabled:
            return
        bals = target.safe_balances(force_refresh=True) or {}
        spot_free = float((bals.get('spot', {}).get('USDT') or {}).get('free') or 0)
        if spot_free <= cfg.earn_idle_threshold_usdt:
            return
        amount = max(0.0, spot_free - 0.10)
        ok, err = target.earn_subscribe(amount, paper_mode=False)
        if ok:
            log_event(db, f'Event-driven sweep: {amount:.2f} USDT spot → earn', mode=mode, exchange=exchange)
        else:
            log_event(db, f'Event-driven sweep blocked: {amount:.2f} USDT — {err}', mode=mode, level='WARN', exchange=exchange)
        db.commit()


bus.subscribe('position_closed', _earn_sweep_for_venue)
bus.subscribe('deposit_detected', _earn_sweep_for_venue)


def get_earn_state(db, mode: str, exchange: str = 'binance') -> EarnState:
    """Per-(mode, venue) earn tracker. Each venue gets its own row so the
    Binance flexible balance and KuCoin funding-wallet balance don't
    overwrite each other on the same shared key."""
    state = db.scalar(select(EarnState).where(
        EarnState.mode == mode,
        EarnState.exchange == exchange,
    ))
    if state is None:
        state = EarnState(mode=mode, exchange=exchange, deployed_usdt=0.0, cumulative_yield_usdt=0.0)
        db.add(state)
        db.flush()
    return state


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


def get_all_earn_states(db, mode: str) -> list[EarnState]:
    """Every venue's EarnState row for ``mode``. Used by the dashboard to
    aggregate earn balances across venues without missing any."""
    return list(db.scalars(select(EarnState).where(EarnState.mode == mode)).all())


def _accrue_paper_yield(earn: EarnState, apr: float) -> None:
    """For paper mode only — compound interest at the configured APR over the elapsed wall time."""
    now = datetime.utcnow()
    elapsed = (now - earn.last_accrual_ts).total_seconds()
    if elapsed <= 0 or earn.deployed_usdt <= 0 or apr <= 0:
        earn.last_accrual_ts = now
        return
    rate_per_second = apr / (365.0 * 24 * 3600)
    yield_amt = earn.deployed_usdt * rate_per_second * elapsed
    earn.deployed_usdt += yield_amt
    earn.cumulative_yield_usdt += yield_amt
    earn.last_accrual_ts = now


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


def _refresh_live_earn_balance(gateway: VenueGateway, earn: EarnState) -> None:
    bal, err = gateway.earn_balance_usdt()
    if bal is not None:
        # We deliberately do NOT synthesize cumulative_yield from balance
        # deltas anymore. The previous design — credit ``min(delta, 1%
        # × deployed)`` per cycle when the balance went up — was supposed
        # to approximate accrual but in practice picked up every internal
        # transfer the bot did (sweep trade→main on KuCoin, mint USDT→
        # BFUSD on Binance). With cycles every 30s, the cumulative figure
        # drifted to absurd values (~$6 on $10 deployed in a few hours,
        # i.e. ~50,000× the realistic Simple Earn rate).
        #
        # Real interest accrual lives in dedicated endpoints — KuCoin's
        # utaPrivateGetAccountInterestHistory and Binance's
        # /sapi/v1/simple-earn/flexible/history/rewardsRecord. Wiring
        # those is the right way; until then cumulative_yield stays at
        # whatever it was (the migration zeros it on each deploy that
        # touches earn_state schema). The dashboard now renders 'not yet
        # measured' for the income line.
        earn.deployed_usdt = bal
        earn.last_error = ''
    elif err:
        earn.last_error = err
    earn.last_accrual_ts = datetime.utcnow()


_LEGACY_PERIOD_SENTINEL = 0.005


def get_strategy_config(db) -> StrategyConfig:
    cfg = db.scalar(select(StrategyConfig).where(StrategyConfig.id == 1))
    if cfg is None:
        cfg = StrategyConfig(id=1)
        db.add(cfg)
        db.flush()
        return cfg
    migrated = False
    if 0 < cfg.entry_funding_threshold < _LEGACY_PERIOD_SENTINEL:
        cfg.entry_funding_threshold = round(cfg.entry_funding_threshold * 1095.0, 6)
        migrated = True
    if 0 < cfg.exit_funding_threshold < _LEGACY_PERIOD_SENTINEL:
        cfg.exit_funding_threshold = round(cfg.exit_funding_threshold * 1095.0, 6)
        migrated = True
    if migrated:
        log_event(db, f'Migrated funding thresholds to APR: entry={cfg.entry_funding_threshold:.4f}, exit={cfg.exit_funding_threshold:.4f}', mode=MODE_PAPER, exchange='system')
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
    """Walk all three wallets (spot, futures, earn) and reshuffle capital so the
    close has both legs funded. Live only — paper has no separate wallets.

    Spot leg: needs `p.quantity` of the base asset on spot. If short, redeems
    that exact deficit from the base asset's flexible Earn product.

    Perp leg buy-back: needs futures.free ≥ position_notional / leverage + a
    small fee buffer. Binance's order-placement check requires fresh initial
    margin even on a reducing trade, which is why a stuck close looks like
    "Margin is insufficient" even though the position is small. Funds the gap
    by, in order: surplus spot USDT → spot→futures transfer; then, if still
    short, USDT redeem from Earn → spot → transfer.

    Best-effort throughout — each step is wrapped, and the actual close attempt
    that runs after this is the source of truth for whether we succeeded.
    Logs only when an action actually fires (no spam if pre-flight is a no-op
    or if global capital is genuinely insufficient)."""
    if p.mode != MODE_LIVE:
        return

    bals = gateway.safe_balances() or {}
    base = p.spot_symbol.split('/')[0]

    # ---- Spot asset side ----
    spot_asset_qty = float((bals.get('spot', {}).get(base) or {}).get('total') or 0)
    if cfg.earn_subscribe_spot_assets and spot_asset_qty < p.quantity * 0.99:
        deficit = max(0.0, p.quantity - spot_asset_qty)
        if deficit > 0:
            ok, err = gateway.earn_redeem_asset(base, deficit, False)
            if ok:
                log_event(db, f'Pre-close: redeemed {deficit:.6f} {base} from Earn', mode=p.mode, exchange=p.exchange)
            elif BINANCE_ERR_NO_EARN_POSITION in err or "doesn't exist" in err.lower() or 'no flexible product' in err.lower():
                pass  # nothing was subscribed; close_spot will use whatever's already in spot
            else:
                log_event(db, f'Pre-close: redeem {base} for {p.perp_symbol} failed: {err[:120]}', mode=p.mode, level='WARN', exchange=p.exchange)

    # ---- Perp leg margin side ----
    fut_free = float((bals.get('futures', {}).get('USDT') or {}).get('free') or 0)
    spot_free = float((bals.get('spot', {}).get('USDT') or {}).get('free') or 0)
    perp_now = gateway.safe_price(p.perp_symbol, perp=True) or p.perp_entry_price or 0.0
    if perp_now <= 0:
        return  # can't size the requirement; the close attempt will surface the failure
    leverage = max(1, cfg.max_perp_leverage or cfg.perp_leverage or 1)
    needed_margin = p.quantity * perp_now / leverage
    target = needed_margin * 1.005  # +50 bps buffer for fee + tiny adverse drift
    if fut_free >= target:
        return

    gap = target - fut_free

    # Step 1: surplus spot USDT → futures
    if cfg.auto_transfer_enabled and spot_free > 0.30:
        avail = max(0.0, spot_free - 0.10)  # leave dust in spot
        transfer = min(gap, avail)
        if transfer >= 0.20:
            ok, err = gateway.transfer_spot_to_futures(transfer, False)
            if ok:
                spot_free -= transfer
                fut_free += transfer
                gap -= transfer
                log_event(db, f'Pre-close: transferred {transfer:.2f} USDT spot→futures (margin top-up for {p.perp_symbol})', mode=p.mode, exchange=p.exchange)
            else:
                log_event(db, f'Pre-close: spot→futures transfer for {p.perp_symbol} failed: {err[:120]}', mode=p.mode, level='WARN', exchange=p.exchange)

    # Step 2: redeem USDT from Earn → spot → futures
    if gap >= 0.20 and cfg.earn_enabled and cfg.auto_transfer_enabled:
        earn_bal, _ = gateway.earn_balance_usdt()
        earn_bal = earn_bal or 0.0
        if earn_bal >= 0.20:
            redeem_amt = min(gap + 0.20, earn_bal)  # tiny extra so subsequent transfer has room
            ok, err = gateway.earn_redeem(redeem_amt, False)
            if ok:
                log_event(db, f'Pre-close: redeemed {redeem_amt:.2f} USDT from Earn (margin top-up for {p.perp_symbol})', mode=p.mode, exchange=p.exchange)
                transfer = min(gap, redeem_amt)
                if transfer >= 0.20:
                    ok2, err2 = gateway.transfer_spot_to_futures(transfer, False)
                    if ok2:
                        log_event(db, f'Pre-close: transferred {transfer:.2f} USDT to futures (Earn → spot → futures)', mode=p.mode, exchange=p.exchange)
                    else:
                        log_event(db, f'Pre-close: post-redeem transfer for {p.perp_symbol} failed: {err2[:120]}', mode=p.mode, level='WARN', exchange=p.exchange)


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


def _compute_equity_and_free(db, gateway: VenueGateway, mode: str, cfg: StrategyConfig, earn: EarnState) -> tuple[float, float]:
    """Total portfolio equity in USDT and the amount currently free for opening
    a position **on this venue**.

    Per-venue scoping: every query filters by ``Position.exchange == gateway.venue_id``
    so two venues sharing the same StrategyConfig can size positions correctly
    against their own capital. Cross-venue capital movement is Phase 2.

    Paper mode:  total = paper_starting_equity + manual_flows + realized + unrealized
                       + open_funding + closed_funding + earn_yield
                 free  = total − earn_deployed − open_notional − unrealized
    Live mode:   total = spot.USDT + fut.USDT + Σ (non-USDT spot assets × ticker)
                       + venue's Earn balance
                 free  = min(spot.free, fut.free)   (both legs need margin)
    """
    if mode == MODE_PAPER:
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
        # Funding on already-closed positions is part of total_realized_pnl (added
        # below); open positions' accrued funding is tracked separately so it can
        # be treated as liquid cash for the free-balance calc — this is what
        # makes "auto-reinvest" work.
        closed_funding = total_funding_income(db, mode=mode, status='closed', exchange=gateway.venue_id)
        # Manual capital flows are scoped per-venue (default 'binance' on legacy rows).
        capital_in = db.scalar(select(func.coalesce(func.sum(CapitalFlow.amount_usdt), 0.0)).where(
            CapitalFlow.mode == mode,
            CapitalFlow.exchange == gateway.venue_id,
        )) or 0.0
        total_equity = (cfg.paper_starting_equity + capital_in + realized + unrealized
                        + open_funding + closed_funding + earn.cumulative_yield_usdt)
        free = max(0.0, total_equity - earn.deployed_usdt - open_notional - unrealized)
        return total_equity, free
    # Live mode — every value comes from the venue's own balances API.
    bals = gateway.safe_balances()
    if bals is None:
        return 0.0, 0.0
    spot_total = float((bals['spot'].get('USDT') or {}).get('total') or 0)
    fut_total = float((bals['futures'].get('USDT') or {}).get('total') or 0)
    spot_free = float((bals['spot'].get('USDT') or {}).get('free') or 0)
    fut_free = float((bals['futures'].get('USDT') or {}).get('free') or 0)
    # IMPORTANT: spot.USDT.total only counts USDT — base assets the bot bought
    # on the spot leg (SOL, ETH, etc.) live in their own balance entries.
    # Without adding them back at USDT-equivalent, equity appears to drop by
    # the full position notional every time we open a trade.
    spot_assets_value = 0.0
    spot_balances = bals.get('spot', {}) or {}
    META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
    for asset, bal in spot_balances.items():
        if asset in META_KEYS or asset == 'USDT' or not isinstance(bal, dict):
            continue
        qty = float(bal.get('total') or 0)
        if qty <= 0:
            continue
        px = gateway.safe_price(f'{asset}/USDT') or 0
        spot_assets_value += qty * px
    total_equity = spot_total + fut_total + spot_assets_value + earn.deployed_usdt
    return total_equity, min(spot_free, fut_free)


# Per-(mode, venue) timestamp of the last capital-flow ingest. The bot
# loop calls _maybe_ingest_capital_flows every cycle but the actual SAPI
# walk only fires once per hour — capital-flow rows don't change between
# cycles unless the user moves money, and the chunked walk eats several
# rate-limited requests. Manual triggers (the dashboard's "Re-ingest"
# button) bypass the throttle.
_CAPITAL_INGEST_INTERVAL_S = 3600.0
_LAST_CAPITAL_INGEST_AT: dict[tuple[str, str], float] = {}


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
        # Fire one event per inflow so subscribers (earn-sweep) can route
        # the new cash without waiting for the next cycle. Outflows
        # don't trigger anything — the wallet is already short by the
        # withdrawal amount; nothing to sweep.
        if r['amount'] > 0:
            bus.emit('deposit_detected',
                     mode=mode, exchange=gateway.venue_id,
                     amount_usdt=r['amount'], kind=r.get('kind') or 'deposit', ts=r['ts'])
    if inserted:
        db.flush()
    summary = ', '.join(f'{k}={v}' for k, v in counts.items()) or 'no rows returned by any endpoint'
    log_event(db, f'capital-flow ingest: {summary} · {inserted} new', mode=mode, exchange=gateway.venue_id)
    return inserted


def _take_balance_snapshot(db, gateway: VenueGateway, mode: str, cfg: StrategyConfig, earn: EarnState) -> BalanceSnapshot:
    # Both modes share the live/paper-aware equity calc which already includes
    # spot asset values for tracked positions in live mode.
    total_equity, _ = _compute_equity_and_free(db, gateway, mode, cfg, earn)
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
            earn = get_earn_state(db, mode, exchange=gateway.venue_id)

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
                            f'or missing Spot/Futures/Earn permission.',
                            mode=MODE_LIVE,
                            level='ERROR',
                        )
                        _LIVE_API_UNHEALTHY_LOGGED = True
                    db.commit()
                    return
                elif _LIVE_API_UNHEALTHY_LOGGED:
                    log_event(db, f'{gateway.name} API recovered.', mode=MODE_LIVE, level='INFO', exchange=gateway.venue_id)
                    _LIVE_API_UNHEALTHY_LOGGED = False

            # Earn upkeep: accrue paper-mode interest or fetch the live deployed balance.
            if cfg.earn_enabled:
                if paper:
                    _accrue_paper_yield(earn, cfg.earn_paper_apr)
                else:
                    _refresh_live_earn_balance(gateway, earn)

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

            # Live short-circuit: if entries are off, no positions, and no earn or
            # auto-transfer to do, skip the API hits entirely.
            if (mode == MODE_LIVE and not mstate.entry_enabled and not mstate.maintenance_mode
                    and not cfg.earn_enabled and not cfg.auto_transfer_enabled):
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
                    current_funding = gateway.futures.fetch_funding_rates()
                except Exception:
                    current_funding = {}
                for p in open_positions:
                    row = current_funding.get(p.perp_symbol) or {}
                    fr = row.get('fundingRate')
                    if fr is not None:
                        p.last_funding_rate = float(fr)
                        p.funding_interval_hours = _interval_hours(row)
                    current_apr = annualize_rate(p.last_funding_rate, p.funding_interval_hours or 8.0)
                    age = datetime.utcnow() - p.opened_at
                    spot_now = gateway.safe_price(p.spot_symbol)
                    perp_now = gateway.safe_price(p.perp_symbol, perp=True)
                    exit_reason = None
                    mandatory = False
                    if current_apr < cfg.exit_funding_threshold:
                        exit_reason = 'funding_below_threshold'
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
                        cfg.min_24h_quote_volume,
                        min_depth_usdt=cfg.min_order_book_depth_usdt or 0.0,
                        depth_band_bps=cfg.depth_band_bps or 10.0,
                        include_earn_apr=bool(cfg.earn_subscribe_spot_assets),
                    )
                except Exception as e:
                    passing, candidates_total, rejected_scan = [], 0, []
                    log_event(db, f'scan_funding failed: {e}', mode=mode, level='ERROR', exchange=gateway.venue_id)
                candidates_passing = len(passing)
                top_candidates_payload = [{
                    'perp': c.perp_symbol,
                    'fr': c.funding_rate,
                    'apr': c.funding_apr,
                    'interval_h': c.funding_interval_hours,
                    'qv': c.quote_volume,
                    'spot_depth': c.spot_depth_usdt,
                    'perp_depth': c.perp_depth_usdt,
                    'spot_earn_apr': c.spot_earn_apr,
                    'combined_apy': c.combined_apy,
                } for c in passing[:5]]
                for sym, reason, apr in rejected_scan[:20]:
                    db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=sym, reason=reason, funding_rate=apr))

                total_equity, free = _compute_equity_and_free(db, gateway, mode, cfg, earn)
                # Pull per-wallet free balances so we can act on each leg's constraint
                # individually (live only — paper has a single pool).
                if mode == MODE_LIVE:
                    bals = gateway.safe_balances() or {}
                    spot_free = float((bals.get('spot', {}).get('USDT') or {}).get('free') or 0)
                    fut_free = float((bals.get('futures', {}).get('USDT') or {}).get('free') or 0)
                else:
                    spot_free = fut_free = free

                if total_equity <= 0:
                    scan_action = 'balances_unavailable'
                    scan_note = 'safe_balances() returned None or paper equity = 0'
                else:
                    scan_action = 'no_fill'
                    desired_notional = cfg.max_position_pct * total_equity
                    min_notional = cfg.min_position_pct * total_equity

                    # Top up spot from Earn (once per cycle, not per candidate).
                    if cfg.earn_enabled and spot_free < desired_notional and earn.deployed_usdt > 0.10:
                        need = min(desired_notional - spot_free, earn.deployed_usdt)
                        if need >= 0.10:
                            ok, err = gateway.earn_redeem(need, paper)
                            if ok:
                                earn.deployed_usdt -= need
                                spot_free += need
                                log_event(db, f'Redeemed {need:.4f} USDT from earn before opening', mode=mode, exchange=gateway.venue_id)
                            else:
                                earn.last_error = err
                                log_event(db, f'Earn redeem USDT (need={need:.4f}) failed: {err}', mode=mode, level='WARN', exchange=gateway.venue_id)

                    # Transfer spot→futures so the perp leg has margin (once per cycle).
                    if mode == MODE_LIVE and cfg.auto_transfer_enabled and fut_free < desired_notional and spot_free > desired_notional:
                        transfer = min(desired_notional - fut_free, spot_free - desired_notional)
                        if transfer > 0.5:
                            ok, err = gateway.transfer_spot_to_futures(transfer, paper)
                            if ok:
                                spot_free -= transfer
                                fut_free += transfer
                                log_event(db, f'Auto-transferred {transfer:.2f} USDT spot→futures for perp margin', mode=mode, exchange=gateway.venue_id)
                            else:
                                log_event(db, f'spot→futures transfer failed: {err}', mode=mode, level='WARN', exchange=gateway.venue_id)

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
                        # Size based on multiple binding constraints:
                        #  * Equity / wallet:  free cash on the binding leg
                        #  * Strategy cap:     desired_notional (% of equity)
                        #  * Book depth:       at most 25% of the tightest
                        #                      side's depth at the entry band.
                        #                      Single-fill slippage past 25%
                        #                      of book starts being material.
                        #  * 24h volume:       at most 0.5% of quote volume.
                        #                      Concentration above this means
                        #                      our exits move the market.
                        free_for_arb = min(spot_free, fut_free)
                        wallet_cap = free_for_arb * 0.97
                        depth_cap = 0.25 * (c.min_depth_usdt or 0)
                        volume_cap = 0.005 * (c.quote_volume or 0)
                        # If depth/volume readings are zero (scan didn't
                        # populate), don't let them clamp to zero —
                        # min_depth_usdt is a hard reject upstream when it
                        # actually fails. Treat 0 as "unknown, no constraint".
                        if depth_cap <= 0:
                            depth_cap = float('inf')
                        if volume_cap <= 0:
                            volume_cap = float('inf')
                        sized_notional = min(wallet_cap, desired_notional, depth_cap, volume_cap)
                        if sized_notional < min_notional:
                            reason = (f'below min position pct ({sized_notional:.2f} < {min_notional:.2f} USDT; '
                                      f'wallet={wallet_cap:.2f} max_pct={desired_notional:.2f} '
                                      f'depth_cap={depth_cap if depth_cap != float("inf") else "—"} '
                                      f'vol_cap={volume_cap if volume_cap != float("inf") else "—"})')
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol, reason=reason, funding_rate=c.funding_apr))
                            scan_action = 'below_min_pct'
                            continue
                        spot_px = gateway.safe_price(c.spot_symbol) or 0
                        perp_px = gateway.safe_price(c.perp_symbol, perp=True) or 0
                        if spot_px <= 0 or perp_px <= 0:
                            continue
                        ok, entry_bps = is_basis_entry_acceptable(spot_px, perp_px, cfg.max_entry_basis_bps)
                        if not ok:
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol, reason=f'basis_too_wide ({entry_bps:+.1f}bps > ±{cfg.max_entry_basis_bps:.1f})', funding_rate=c.funding_apr))
                            scan_action = 'basis_too_wide'
                            continue
                        qty = sized_notional / max(0.0001, spot_px)
                        # Place spot buy first. If it fails (insufficient balance, min-notional,
                        # market down), skip the candidate and continue scanning.
                        try:
                            s = gateway.create_spot_buy(c.spot_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                        except Exception as e:
                            err = str(e)[:140]
                            log_event(db, f'Spot buy failed for {c.spot_symbol}: {err}', mode=mode, level='ERROR', exchange=gateway.venue_id)
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol, reason=f'spot_buy_error: {err[:80]}', funding_rate=c.funding_apr))
                            scan_action = 'spot_buy_error'
                            continue

                        # Spot succeeded. Persist the half-built position immediately so the
                        # rollback path (or next-cycle hedge check) has a row to attach to.
                        pos = Position(
                            mode=mode,
                            exchange=gateway.venue_id,
                            trade_type=venue_to_trade_type(gateway.venue_id),
                            symbol=c.spot_symbol.split('/')[0],
                            spot_symbol=c.spot_symbol,
                            perp_symbol=c.perp_symbol,
                            quantity=qty,
                            entry_funding_rate=c.funding_rate,
                            last_funding_rate=c.funding_rate,
                            funding_interval_hours=c.funding_interval_hours,
                            spot_entry_price=float(s.get('price') or 0),
                            perp_entry_price=0.0,
                            last_funding_accrual_ts=datetime.utcnow(),
                        )
                        db.add(pos)
                        db.flush()
                        record_trade(db, pos.id, mode, c.spot_symbol, 'spot', 'buy', qty, s, exchange=gateway.venue_id)

                        # Now the perp short. Configure CROSS margin + 1x leverage on the
                        # symbol first — keeps the perp's used margin == its notional and
                        # avoids "Margin is insufficient" surprises later. Idempotent.
                        if not paper:
                            cfg_ok, cfg_err = gateway.configure_perp_for_arb(c.perp_symbol)
                            if not cfg_ok:
                                log_event(db, f'configure_perp_for_arb({c.perp_symbol}) warned: {cfg_err[:120]}', mode=mode, level='WARN', exchange=gateway.venue_id)
                        try:
                            f = gateway.create_perp_short(c.perp_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                        except Exception as e:
                            err = str(e)[:140]
                            log_event(db, f'Perp short failed for {c.perp_symbol} after spot bought: {err} — rolling back spot leg', mode=mode, level='ERROR', exchange=gateway.venue_id)
                            try:
                                if not paper and cfg.earn_subscribe_spot_assets:
                                    base = c.spot_symbol.split('/')[0]
                                    gateway.earn_redeem_asset(base, qty, False)
                                rev = gateway.close_spot(c.spot_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                                record_trade(db, pos.id, mode, c.spot_symbol, 'spot', 'sell', qty, rev, exchange=gateway.venue_id)
                                pos.status = 'closed'
                                pos.closed_at = datetime.utcnow()
                                log_event(db, f'Rolled back spot leg of {c.perp_symbol}; loss ≈ entry+exit fees + slippage', mode=mode, exchange=gateway.venue_id)
                            except Exception as e2:
                                log_event(db, f'CRITICAL: spot rollback failed: {str(e2)[:140]}. {c.spot_symbol} is naked-long; hedge check will retry next cycle.', mode=mode, level='ERROR', exchange=gateway.venue_id)
                            db.add(RejectedCandidate(mode=mode, exchange=gateway.venue_id, symbol=c.perp_symbol, reason=f'perp_short_error: {err[:80]}', funding_rate=c.funding_apr))
                            scan_action = 'perp_short_error'
                            continue

                        # Spot leg → Earn (opt-in). Skipping is fine if no flexible product
                        # exists for the asset; the asset just sits in spot wallet.
                        if not paper and cfg.earn_subscribe_spot_assets:
                            base = c.spot_symbol.split('/')[0]
                            ok_e, err_e = gateway.earn_subscribe_asset(base, qty, False)
                            if ok_e:
                                log_event(db, f'Subscribed {qty:.6f} {base} to flexible Earn ({c.perp_symbol})', mode=mode, exchange=gateway.venue_id)
                            elif 'cooldown active' in err_e:
                                log_event(db, f'Earn subscribe for {base} skipped (cooldown — {c.perp_symbol})', mode=mode, exchange=gateway.venue_id)
                            else:
                                log_event(db, f'Earn subscribe for {base} on {c.perp_symbol} skipped: {err_e[:120]}', mode=mode, level='WARN', exchange=gateway.venue_id)

                        # Both legs filled — finalize.
                        pos.perp_entry_price = float(f.get('price') or 0)
                        record_trade(db, pos.id, mode, c.perp_symbol, 'futures', 'sell', qty, f, exchange=gateway.venue_id)
                        held_bases.add(base)
                        scan_action = f'opened {c.perp_symbol}'
                        log_event(db, f'Opened {c.perp_symbol} qty={qty:.6f} funding_apy={c.funding_apr:.2%} earn_apr={c.spot_earn_apr:.2%} combined={c.combined_apy:.2%} depth=${c.min_depth_usdt:.0f}', mode=mode, exchange=gateway.venue_id)
                        bus.emit('position_opened',
                                 position_id=pos.id, mode=mode, exchange=gateway.venue_id,
                                 symbol=c.spot_symbol, quantity=qty,
                                 notional=qty * pos.spot_entry_price)
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

            # Earn-first model: idle USDT lives in earn between trades.
            # Pre-trade provision_margin redeems from earn to top up
            # whichever wallet the upcoming leg needs.
            #
            # Drain rules (live mode):
            #   * No positions open on the venue → drain fut→spot down to
            #     dust (0.10 USDT). Everything funnels into earn.
            #   * Positions open → drain only the portion of fut.free that
            #     exceeds 10% of total open notional. Below that threshold
            #     we leave a free-margin buffer so a price move against
            #     the perp short doesn't trigger maintenance liquidation
            #     before the next provision_margin call.
            if mode == MODE_LIVE and cfg.auto_transfer_enabled:
                open_ps = db.scalars(select(Position).where(
                    Position.status == 'open',
                    Position.mode == mode,
                    Position.exchange == gateway.venue_id,
                )).all()
                bals_after = gateway.safe_balances() or {}
                fut_free_now = float((bals_after.get('futures', {}).get('USDT') or {}).get('free') or 0)
                # Under unified margin (Binance PM, KuCoin UTA), the pool
                # funds both spot and perp legs — there's no separate
                # futures wallet to drain. ``futures.USDT.total`` is 0 in
                # those cases by convention; skip the drain entirely so
                # we don't generate spurious "Drained X USDT" log lines.
                fut_total_now = float((bals_after.get('futures', {}).get('USDT') or {}).get('total') or 0)
                if fut_total_now <= 0.001:
                    fut_free_now = 0.0  # disables the drain branch below
                if open_ps:
                    # Keep ``cfg.futures_buffer_pct`` of open notional as free
                    # margin in the futures wallet so the perp short can absorb
                    # an adverse move before maintenance liquidation. 20% (the
                    # default) covers roughly a -20% mark-price move; bump
                    # higher in cfg for high-volatility tokens. Cross margin
                    # pools the buffer across all open perps on the venue.
                    open_notional = sum((p.quantity or 0) * (p.perp_entry_price or 0) for p in open_ps)
                    keep = max(0.20, open_notional * float(cfg.futures_buffer_pct or 0.20))
                else:
                    keep = 0.10
                if fut_free_now > keep + 0.10:
                    amt = fut_free_now - keep
                    ok, err = gateway.transfer_futures_to_spot(amt, paper)
                    if ok:
                        log_event(db, f'Drained {amt:.2f} USDT futures→spot (kept {keep:.2f} as margin buffer; {len(open_ps)} pos open)', mode=mode, exchange=gateway.venue_id)
                    else:
                        log_event(db, f'futures→spot drain failed: {err}', mode=mode, level='WARN', exchange=gateway.venue_id)

            # Sweep idle USDT into earn. Use ``spot.free`` directly rather
            # than ``min(spot_free, fut_free)`` from _compute_equity_and_free
            # — that minimum was the right shape for the old continuous-
            # rebalance model where both legs needed equal margin, but in
            # the earn-first model the spot wallet's free cash IS the idle
            # amount (futures cash gets routed pre-trade by provision_margin
            # rather than kept hot). The old min() left 7.90 in trade
            # because fut had only 0.10 — fix is using spot.free directly.
            if cfg.earn_enabled:
                bals_for_sweep = gateway.safe_balances() or {}
                spot_free_now = float((bals_for_sweep.get('spot', {}).get('USDT') or {}).get('free') or 0)
                if spot_free_now > cfg.earn_idle_threshold_usdt:
                    sweep = max(0.0, spot_free_now - 0.10)  # 0.10 USDT dust buffer
                    # Binance-only safety throttle: cap BFUSD share of total
                    # equity at cfg.binance_max_bfusd_pct. Keeps a portion in
                    # plain USDT for instant deploy without the BFUSD redeem
                    # queue. Wire is no-op on KuCoin since auto-lent USDT
                    # stays liquid as cross-collateral anyway.
                    if gateway.venue_id == 'binance' and cfg.binance_max_bfusd_pct < 1.0:
                        total_eq, _ = _compute_equity_and_free(db, gateway, mode, cfg, earn)
                        cap = max(0.0, total_eq * float(cfg.binance_max_bfusd_pct or 0.20) - earn.deployed_usdt)
                        if sweep > cap:
                            sweep = cap
                    if sweep <= 0.10:
                        # After cap, nothing meaningful to sweep this cycle.
                        sweep = 0.0
                    ok, err = (True, 'capped') if sweep <= 0 else gateway.earn_subscribe(sweep, paper)
                    if ok:
                        earn.deployed_usdt += sweep
                        earn.last_error = ''
                        log_event(db, f'Swept {sweep:.2f} USDT idle → earn', mode=mode, exchange=gateway.venue_id)
                    else:
                        # Always log the failure reason at WARN. The user
                        # should see *why* an idle balance isn't sweeping
                        # (cooldown countdown, missing earn product, missing
                        # API permission, etc.) on /logs without having to
                        # cross-reference dashboard tooltips.
                        earn.last_error = err
                        log_event(db, f'Earn sweep blocked: {sweep:.2f} USDT idle in spot — {err}', mode=mode, level='WARN', exchange=gateway.venue_id)

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
            snap = _take_balance_snapshot(db, gateway, mode, cfg, earn)
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


def _enforce_venue_yield_settings(gateways: list, db) -> None:
    """One-shot per-startup: push each venue's yield surface into the
    state the operator wants. Idempotent — calling repeatedly with the
    same flags just confirms the existing setting.

    KuCoin: toggles auto-lend for USDT according to
    ``cfg.kucoin_auto_lend_enabled``. The sub-account API key calls
    /api/v1/margin/toggle-auto-lend directly — the user doesn't need
    UI access on the sub-account (which they can't get when master
    settings don't propagate). Auto-lent USDT continues to count as
    margin collateral under UTA, so the position-opening flow is
    unaffected.

    Binance: BFUSD mint is event-driven (post-trade earn sweep) — no
    one-shot toggle needed."""
    cfg = get_strategy_config(db)
    for gw in gateways:
        if gw.venue_id == 'kucoin':
            target_enabled = bool(cfg.kucoin_auto_lend_enabled)
            ok, err = gw.toggle_auto_lend(enabled=target_enabled, asset='USDT')
            if ok:
                log_event(db, f'KuCoin auto-lend USDT: {"ENABLED" if target_enabled else "DISABLED"} via /margin/toggle-auto-lend', mode=MODE_LIVE, exchange=gw.venue_id)
            else:
                log_event(db, f'KuCoin auto-lend toggle failed: {err}', mode=MODE_LIVE, level='WARN', exchange=gw.venue_id)


def run_loop() -> None:
    gateways = make_gateways()
    for gw in gateways:
        try:
            gw.load_markets()
        except Exception as e:
            with SessionLocal() as db:
                log_event(db, f'{gw.name} load_markets failed: {e}', mode=MODE_PAPER, level='ERROR', exchange=gw.venue_id)
                db.commit()
    with SessionLocal() as db:
        _enforce_venue_yield_settings(gateways, db)
        db.commit()
    for gw in gateways:
        reconcile_positions(gw)
    while True:
        sleep_seconds = run_one_cycle(gateways)
        time.sleep(sleep_seconds)
