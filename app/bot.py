from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.exchange import BinanceGateway, _interval_hours, annualize_rate
from app.finance import position_realized_pnl, position_unrealized_pnl, total_realized_pnl
from app.models import (
    ALL_MODES,
    MODE_LIVE,
    MODE_PAPER,
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
)
from app.safety import (
    check_hedge,
    check_market_health,
    is_basis_entry_acceptable,
    is_basis_exit_favorable,
)


CAPITAL_FLOW_THRESHOLD_USDT = 1.0  # was 50 — too coarse for the typical $20 starting equity

# Module-level dedup so we only log "live API down" once per outage instead of every cycle.
_LIVE_API_UNHEALTHY_LOGGED = False

# Per-position close error dedup. Same (position_id, leg, error_msg) tuple suppresses
# a duplicate ERROR row on consecutive cycles. Cleared when the close succeeds.
_CLOSE_ERROR_CACHE: dict[tuple[int, str], str] = {}


def log_event(db, message: str, mode: str = MODE_PAPER, level: str = 'INFO'):
    db.add(BotEvent(mode=mode, level=level, message=message))


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


def get_earn_state(db, mode: str) -> EarnState:
    state = db.scalar(select(EarnState).where(EarnState.mode == mode))
    if state is None:
        state = EarnState(mode=mode, deployed_usdt=0.0, cumulative_yield_usdt=0.0)
        db.add(state)
        db.flush()
    return state


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


def _accrue_paper_funding(db, mode: str) -> None:
    """Linearly accrue funding income on every open paper position based on the wall-clock
    elapsed since last accrual. Funding payments are real cash that lands in the trader's
    wallet — credit them so the next cycle's % sizing can deploy the larger equity."""
    if mode != MODE_PAPER:
        return
    now = datetime.utcnow()
    for p in db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode)).all():
        last = p.last_funding_accrual_ts or p.opened_at
        elapsed_seconds = (now - last).total_seconds()
        if elapsed_seconds <= 0:
            continue
        interval_h = p.funding_interval_hours or 8.0
        period_seconds = interval_h * 3600.0
        if period_seconds <= 0:
            continue
        # Linear accrual within a period — the per-second slice of one funding payment.
        # Notional reference: spot_entry × qty (close enough; real funding is on mark price × qty).
        notional = p.spot_entry_price * p.quantity
        income = notional * p.last_funding_rate * (elapsed_seconds / period_seconds)
        p.funding_income_accrued += income
        p.last_funding_accrual_ts = now


def _refresh_live_earn_balance(gateway: BinanceGateway, earn: EarnState) -> None:
    bal, err = gateway.earn_balance_usdt()
    if bal is not None:
        # Compute realized yield since last refresh (deposits/redeems make this approximate
        # but still useful as a directional indicator).
        delta = bal - earn.deployed_usdt
        if delta > 0:
            earn.cumulative_yield_usdt += delta
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
        log_event(db, f'Migrated funding thresholds to APR: entry={cfg.entry_funding_threshold:.4f}, exit={cfg.exit_funding_threshold:.4f}', mode=MODE_PAPER)
        db.flush()
    return cfg


def record_trade(db, position_id: int | None, mode: str, symbol: str, venue: str, side: str, qty: float, order: dict):
    db.add(Trade(mode=mode, position_id=position_id, symbol=symbol, venue=venue, side=side, quantity=qty, price=float(order.get('price') or 0), fee=float((order.get('fee') or {}).get('cost') or 0)))


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
    log_event(db, f'Failed to close {leg} leg of {p.perp_symbol}: {snippet} (will retry next cycle)', mode=p.mode, level='ERROR')


def _ensure_close_readiness(db, gateway: BinanceGateway, p: Position, cfg: StrategyConfig) -> None:
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
                log_event(db, f'Pre-close: redeemed {deficit:.6f} {base} from Earn', mode=p.mode)
            elif '-6053' in err or "doesn't exist" in err.lower() or 'no flexible product' in err.lower():
                pass  # nothing was subscribed; close_spot will use whatever's already in spot
            else:
                log_event(db, f'Pre-close: redeem {base} failed: {err[:120]}', mode=p.mode, level='WARN')

    # ---- Perp leg margin side ----
    fut_free = float((bals.get('futures', {}).get('USDT') or {}).get('free') or 0)
    spot_free = float((bals.get('spot', {}).get('USDT') or {}).get('free') or 0)
    perp_now = gateway.safe_price(p.perp_symbol, perp=True) or p.perp_entry_price or 0.0
    if perp_now <= 0:
        return  # can't size the requirement; the close attempt will surface the failure
    leverage = max(1, cfg.perp_leverage or 1)
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
                log_event(db, f'Pre-close: transferred {transfer:.2f} USDT spot→futures (margin top-up for {p.perp_symbol})', mode=p.mode)
            else:
                log_event(db, f'Pre-close: spot→futures transfer failed: {err[:120]}', mode=p.mode, level='WARN')

    # Step 2: redeem USDT from Earn → spot → futures
    if gap >= 0.20 and cfg.earn_enabled and cfg.auto_transfer_enabled:
        earn_bal, _ = gateway.earn_balance_usdt()
        earn_bal = earn_bal or 0.0
        if earn_bal >= 0.20:
            redeem_amt = min(gap + 0.20, earn_bal)  # tiny extra so subsequent transfer has room
            ok, err = gateway.earn_redeem(redeem_amt, False)
            if ok:
                log_event(db, f'Pre-close: redeemed {redeem_amt:.2f} USDT from Earn (margin top-up for {p.perp_symbol})', mode=p.mode)
                transfer = min(gap, redeem_amt)
                if transfer >= 0.20:
                    ok2, err2 = gateway.transfer_spot_to_futures(transfer, False)
                    if ok2:
                        log_event(db, f'Pre-close: transferred {transfer:.2f} USDT to futures (Earn → spot → futures)', mode=p.mode)
                    else:
                        log_event(db, f'Pre-close: post-redeem transfer failed: {err2[:120]}', mode=p.mode, level='WARN')


def _rebalance_spot_fut(db, gateway: BinanceGateway, cfg: StrategyConfig, mode: str) -> None:
    """Move USDT between spot and futures wallets so both have roughly the same free
    balance — the right baseline for an arb that always opens equal legs. Runs once
    at the end of each live cycle. Threshold avoids ping-ponging on tiny dust."""
    if mode != MODE_LIVE or not cfg.auto_transfer_enabled:
        return
    bals = gateway.safe_balances() or {}
    spot_free = float((bals.get('spot', {}).get('USDT') or {}).get('free') or 0)
    fut_free = float((bals.get('futures', {}).get('USDT') or {}).get('free') or 0)
    diff = spot_free - fut_free
    if abs(diff) < cfg.auto_rebalance_threshold:
        return
    transfer = abs(diff) / 2.0
    if transfer < 0.20:
        return
    if diff > 0:
        ok, err = gateway.transfer_spot_to_futures(transfer, False)
        direction = 'spot→futures'
    else:
        ok, err = gateway.transfer_futures_to_spot(transfer, False)
        direction = 'futures→spot'
    if ok:
        log_event(db, f'Rebalance: {transfer:.2f} USDT {direction} (spot {spot_free:.2f} ↔ fut {fut_free:.2f})', mode=mode)
    else:
        log_event(db, f'Rebalance {direction} failed: {err}', mode=mode, level='WARN')


def _force_close_both(db, gateway: BinanceGateway, p: Position, cfg: StrategyConfig, reason: str) -> None:
    """Close both legs of a position. Wrap each call so a single Binance error
    (insufficient balance, market halted, etc.) doesn't crash the entire cycle.
    If one leg fails to close, the position stays open and check_hedge will
    retry on the next cycle. Errors are deduped per (position, leg)."""
    paper = (p.mode == MODE_PAPER)
    if not paper:
        _ensure_close_readiness(db, gateway, p, cfg)
    spot_ok = perp_ok = False
    try:
        s = gateway.close_spot(p.spot_symbol, p.quantity, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
        record_trade(db, p.id, p.mode, p.spot_symbol, 'spot', 'sell', p.quantity, s)
        spot_ok = True
    except Exception as e:
        _log_close_error(db, p, 'spot', str(e))
    try:
        f = gateway.close_perp(p.perp_symbol, p.quantity, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
        record_trade(db, p.id, p.mode, p.perp_symbol, 'futures', 'buy', p.quantity, f)
        perp_ok = True
    except Exception as e:
        _log_close_error(db, p, 'perp', str(e))
    if spot_ok and perp_ok:
        p.status = 'closed'
        p.closed_at = datetime.utcnow()
        p.last_close_error = ''
        _CLOSE_ERROR_CACHE.pop((p.id, 'spot'), None)
        _CLOSE_ERROR_CACHE.pop((p.id, 'perp'), None)
        log_event(db, f'Closed {p.perp_symbol} ({reason}); realized={position_realized_pnl(db, p):+.4f}', mode=p.mode)
    elif spot_ok or perp_ok:
        # Partial close — surface clearly. Hedge check will recover on the next cycle.
        log_event(db, f'PARTIAL CLOSE on {p.perp_symbol}: {("spot only" if spot_ok else "perp only")} closed; other leg will retry next cycle', mode=p.mode, level='ERROR')


def _close_naked_leg(db, gateway: BinanceGateway, p: Position, cfg: StrategyConfig, surviving_leg: str | None, reason: str) -> None:
    paper = (p.mode == MODE_PAPER)
    closed_ok = False
    try:
        if surviving_leg == 'spot':
            if not paper:
                _ensure_close_readiness(db, gateway, p, cfg)
            s = gateway.close_spot(p.spot_symbol, p.quantity, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
            record_trade(db, p.id, p.mode, p.spot_symbol, 'spot', 'sell', p.quantity, s)
            closed_ok = True
        elif surviving_leg == 'perp':
            f = gateway.close_perp(p.perp_symbol, p.quantity, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
            record_trade(db, p.id, p.mode, p.perp_symbol, 'futures', 'buy', p.quantity, f)
            closed_ok = True
        else:
            closed_ok = True  # no surviving leg to close
    except Exception as e:
        snippet = str(e)[:140]
        p.last_close_error = f'{surviving_leg or "?"}: {snippet}'
        log_event(db, f'Failed to flatten naked {surviving_leg} leg of {p.perp_symbol}: {snippet} (will retry)', mode=p.mode, level='ERROR')
        return
    if closed_ok:
        p.status = 'closed'
        p.closed_at = datetime.utcnow()
        p.last_close_error = ''
        log_event(db, f'Closed naked leg on {p.perp_symbol}: {reason}; flattened {surviving_leg or "no surviving leg"}', mode=p.mode, level='ERROR')


def manual_close(db, gateway: BinanceGateway, p: Position, cfg: StrategyConfig) -> None:
    _force_close_both(db, gateway, p, cfg, 'manual_close')


def reconcile_positions(gateway: BinanceGateway) -> None:
    """Rehydrate any perp positions on Binance that aren't tracked locally as live positions."""
    with SessionLocal() as db:
        for p in gateway.open_perp_positions_raw():
            symbol = p.get('symbol')
            pos_amt = float(p.get('contracts') or p.get('info', {}).get('positionAmt') or 0)
            existing = db.scalar(select(Position).where(Position.perp_symbol == symbol, Position.status == 'open', Position.mode == MODE_LIVE))
            if existing or pos_amt == 0:
                continue
            base = symbol.split('/')[0]
            db.add(Position(mode=MODE_LIVE, symbol=base, spot_symbol=f'{base}/USDT', perp_symbol=symbol, quantity=abs(pos_amt), entry_funding_rate=0.0))
            log_event(db, f'Rehydrated orphan position {symbol} qty={pos_amt}', mode=MODE_LIVE)
        db.commit()


def _compute_equity_and_free(db, gateway: BinanceGateway, mode: str, cfg: StrategyConfig, earn: EarnState) -> tuple[float, float]:
    """Total portfolio equity in USDT and the amount currently free for opening a position.
    For paper, free = total − earn_deployed − sum(open spot notionals). For live, free is the
    smaller of spot.free and futures.free since both legs of an arb need margin; earn money
    sits in a separate Binance wallet so it's already excluded from spot.free."""
    if mode == MODE_PAPER:
        realized = total_realized_pnl(db, mode=mode)
        open_ps = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode)).all()
        unrealized = 0.0
        open_notional = 0.0
        open_funding = 0.0
        for p in open_ps:
            spot_now = gateway.safe_price(p.spot_symbol) or p.spot_entry_price or 0
            perp_now = gateway.safe_price(p.perp_symbol, perp=True) or p.perp_entry_price or 0
            unrealized += position_unrealized_pnl(p, spot_now, perp_now)
            open_notional += p.quantity * p.spot_entry_price
            open_funding += p.funding_income_accrued
        # Funding on already-closed positions is part of total_realized_pnl (added below);
        # open positions' accrued funding is tracked separately so it can be treated as
        # liquid cash for the free-balance calc — this is what makes "auto-reinvest" work.
        closed_funding = db.scalar(select(func.coalesce(func.sum(Position.funding_income_accrued), 0.0)).where(Position.status == 'closed', Position.mode == mode)) or 0.0
        capital_in = db.scalar(select(func.coalesce(func.sum(CapitalFlow.amount_usdt), 0.0)).where(CapitalFlow.mode == mode)) or 0.0
        total_equity = (cfg.paper_starting_equity + capital_in + realized + unrealized
                        + open_funding + closed_funding + earn.cumulative_yield_usdt)
        free = max(0.0, total_equity - earn.deployed_usdt - open_notional - unrealized)
        return total_equity, free
    bals = gateway.safe_balances()
    if bals is None:
        return 0.0, 0.0
    spot_total = float((bals['spot'].get('USDT') or {}).get('total') or 0)
    fut_total = float((bals['futures'].get('USDT') or {}).get('total') or 0)
    spot_free = float((bals['spot'].get('USDT') or {}).get('free') or 0)
    fut_free = float((bals['futures'].get('USDT') or {}).get('free') or 0)
    # IMPORTANT: spot.USDT.total only counts USDT — the base assets the bot
    # bought on the spot leg (SOL, ETH, etc.) live in their own balance
    # entries. Without adding them back as USDT-equivalent, equity appears
    # to drop by the full position notional every time we open a trade.
    # Walk every non-USDT spot balance and price it via the spot ticker.
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


def _take_balance_snapshot(db, gateway: BinanceGateway, mode: str, cfg: StrategyConfig, earn: EarnState) -> BalanceSnapshot:
    # Both modes share the live/paper-aware equity calc which already includes
    # spot asset values for tracked positions in live mode.
    total_equity, _ = _compute_equity_and_free(db, gateway, mode, cfg, earn)
    if mode == MODE_LIVE:
        bals = gateway.safe_balances()
        spot = float((bals['spot'].get('USDT') or {}).get('total') or 0) if bals else 0.0
        fut = float((bals['futures'].get('USDT') or {}).get('total') or 0) if bals else 0.0
        snap = BalanceSnapshot(spot_usdt=spot, futures_usdt=fut, total_usdt=total_equity, source=MODE_LIVE)
    else:
        snap = BalanceSnapshot(spot_usdt=total_equity, futures_usdt=0.0, total_usdt=total_equity, source=mode)
    db.add(snap)
    db.flush()
    return snap


def _detect_capital_flow(db, prev: BalanceSnapshot | None, curr: BalanceSnapshot, mode: str) -> None:
    if mode != MODE_LIVE:
        return  # auto-detection is only meaningful against real exchange balances
    if prev is None or prev.source != curr.source:
        return
    delta = curr.total_usdt - prev.total_usdt
    if abs(delta) < CAPITAL_FLOW_THRESHOLD_USDT:
        return
    trade_count = db.scalar(select(func.count(Trade.id)).where(Trade.ts > prev.ts, Trade.ts <= curr.ts, Trade.mode == mode)) or 0
    if trade_count > 0:
        return
    cf = CapitalFlow(
        mode=mode,
        amount_usdt=delta,
        kind='deposit' if delta > 0 else 'withdrawal',
        detected_by='auto',
        note=f'Auto-detected balance jump of {delta:+.2f} USDT with no trade activity',
    )
    db.add(cf)
    log_event(db, f'Detected capital flow {delta:+.2f} USDT', mode=mode)


def run_one_cycle_for_mode(gateway: BinanceGateway, mode: str) -> None:
    paper = (mode == MODE_PAPER)
    try:
        with SessionLocal() as db:
            cfg = get_strategy_config(db)
            mstate = get_mode_state(db, mode)
            earn = get_earn_state(db, mode)

            # Paper-mode funding income accrual on open positions (live is auto-credited
            # by Binance into the futures wallet, which our equity calc already sees).
            _accrue_paper_funding(db, mode)

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
                    log_event(db, 'Live API recovered.', mode=MODE_LIVE, level='INFO')
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
                        existing = db.scalar(select(Position).where(Position.perp_symbol == sym, Position.status == 'open', Position.mode == MODE_LIVE))
                        if existing:
                            continue
                        base = sym.split('/')[0]
                        db.add(Position(
                            mode=MODE_LIVE, symbol=base,
                            spot_symbol=f'{base}/USDT', perp_symbol=sym,
                            quantity=pos_amt, entry_funding_rate=0.0,
                            last_funding_accrual_ts=datetime.utcnow(),
                        ))
                        log_event(db, f'Rehydrated orphan position {sym} qty={pos_amt}', mode=MODE_LIVE, level='WARN')
                except Exception as e:
                    log_event(db, f'Reconcile failed: {str(e)[:120]}', mode=MODE_LIVE, level='WARN')

            # Live short-circuit: if entries are off, no positions, and no earn or
            # auto-transfer to do, skip the API hits entirely.
            if (mode == MODE_LIVE and not mstate.entry_enabled and not mstate.maintenance_mode
                    and not cfg.earn_enabled and not cfg.auto_transfer_enabled):
                open_count = db.scalar(select(func.count(Position.id)).where(Position.status == 'open', Position.mode == mode)) or 0
                if open_count == 0:
                    return

            # Maintenance — close everything in this mode and disable entries.
            if mstate.maintenance_mode:
                for p in db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode)).all():
                    _force_close_both(db, gateway, p, cfg, 'maintenance')
                if mstate.entry_enabled:
                    mstate.entry_enabled = False
                    log_event(db, 'Maintenance mode active — entries disabled', mode=mode)
                # leave maintenance_mode set; user clears it via the dashboard
                db.commit()
                return

            open_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode)).all()

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
                open_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode)).all()

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
                            log_event(db, f'Exit deferred for {p.perp_symbol} ({exit_reason}): basis={bps:+.1f}bps > max_exit={cfg.max_exit_basis_bps:.1f}bps', mode=mode)
                            continue
                    if exit_reason:
                        _force_close_both(db, gateway, p, cfg, exit_reason)

            open_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode)).all()
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
                    log_event(db, f'scan_funding failed: {e}', mode=mode, level='ERROR')
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
                    db.add(RejectedCandidate(mode=mode, symbol=sym, reason=reason, funding_rate=apr))

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
                                log_event(db, f'Redeemed {need:.4f} USDT from earn before opening', mode=mode)
                            else:
                                earn.last_error = err
                                log_event(db, f'Earn redeem failed: {err}', mode=mode, level='WARN')

                    # Transfer spot→futures so the perp leg has margin (once per cycle).
                    if mode == MODE_LIVE and cfg.auto_transfer_enabled and fut_free < desired_notional and spot_free > desired_notional:
                        transfer = min(desired_notional - fut_free, spot_free - desired_notional)
                        if transfer > 0.5:
                            ok, err = gateway.transfer_spot_to_futures(transfer, paper)
                            if ok:
                                spot_free -= transfer
                                fut_free += transfer
                                log_event(db, f'Auto-transferred {transfer:.2f} USDT spot→futures for perp margin', mode=mode)
                            else:
                                log_event(db, f'spot→futures transfer failed: {err}', mode=mode, level='WARN')

                    # Diversity: skip any candidate whose base asset we already hold open
                    # in this mode. Avoids piling more capital into the same name even
                    # though the perp symbol formally matches; a clean default for the
                    # max_open_positions > 1 case.
                    held_bases = {p.spot_symbol.split('/')[0] for p in db.scalars(
                        select(Position).where(Position.status == 'open', Position.mode == mode)
                    ).all()}
                    for c in passing[:5]:
                        base = c.spot_symbol.split('/')[0]
                        if base in held_bases:
                            continue
                        # Size based on the binding constraint across both legs.
                        free_for_arb = min(spot_free, fut_free)
                        sized_notional = min(free_for_arb * 0.97, desired_notional)
                        if sized_notional < min_notional:
                            reason = f'below min position pct ({sized_notional:.2f} < {min_notional:.2f} USDT; spot_free={spot_free:.2f} fut_free={fut_free:.2f})'
                            db.add(RejectedCandidate(mode=mode, symbol=c.perp_symbol, reason=reason, funding_rate=c.funding_apr))
                            scan_action = 'below_min_pct'
                            continue
                        spot_px = gateway.safe_price(c.spot_symbol) or 0
                        perp_px = gateway.safe_price(c.perp_symbol, perp=True) or 0
                        if spot_px <= 0 or perp_px <= 0:
                            continue
                        ok, entry_bps = is_basis_entry_acceptable(spot_px, perp_px, cfg.max_entry_basis_bps)
                        if not ok:
                            db.add(RejectedCandidate(mode=mode, symbol=c.perp_symbol, reason=f'basis_too_wide ({entry_bps:+.1f}bps > ±{cfg.max_entry_basis_bps:.1f})', funding_rate=c.funding_apr))
                            scan_action = 'basis_too_wide'
                            continue
                        qty = sized_notional / max(0.0001, spot_px)
                        # Place spot buy first. If it fails (insufficient balance, min-notional,
                        # market down), skip the candidate and continue scanning.
                        try:
                            s = gateway.create_spot_buy(c.spot_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                        except Exception as e:
                            err = str(e)[:140]
                            log_event(db, f'Spot buy failed for {c.spot_symbol}: {err}', mode=mode, level='ERROR')
                            db.add(RejectedCandidate(mode=mode, symbol=c.perp_symbol, reason=f'spot_buy_error: {err[:80]}', funding_rate=c.funding_apr))
                            scan_action = 'spot_buy_error'
                            continue

                        # Spot succeeded. Persist the half-built position immediately so the
                        # rollback path (or next-cycle hedge check) has a row to attach to.
                        pos = Position(
                            mode=mode,
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
                        record_trade(db, pos.id, mode, c.spot_symbol, 'spot', 'buy', qty, s)

                        # Now the perp short. Configure CROSS margin + 1x leverage on the
                        # symbol first — keeps the perp's used margin == its notional and
                        # avoids "Margin is insufficient" surprises later. Idempotent.
                        if not paper:
                            cfg_ok, cfg_err = gateway.configure_perp_for_arb(c.perp_symbol)
                            if not cfg_ok:
                                log_event(db, f'configure_perp_for_arb({c.perp_symbol}) warned: {cfg_err[:120]}', mode=mode, level='WARN')
                        try:
                            f = gateway.create_perp_short(c.perp_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                        except Exception as e:
                            err = str(e)[:140]
                            log_event(db, f'Perp short failed for {c.perp_symbol} after spot bought: {err} — rolling back spot leg', mode=mode, level='ERROR')
                            try:
                                if not paper and cfg.earn_subscribe_spot_assets:
                                    base = c.spot_symbol.split('/')[0]
                                    gateway.earn_redeem_asset(base, qty, False)
                                rev = gateway.close_spot(c.spot_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                                record_trade(db, pos.id, mode, c.spot_symbol, 'spot', 'sell', qty, rev)
                                pos.status = 'closed'
                                pos.closed_at = datetime.utcnow()
                                log_event(db, f'Rolled back spot leg of {c.perp_symbol}; loss ≈ entry+exit fees + slippage', mode=mode)
                            except Exception as e2:
                                log_event(db, f'CRITICAL: spot rollback failed: {str(e2)[:140]}. {c.spot_symbol} is naked-long; hedge check will retry next cycle.', mode=mode, level='ERROR')
                            db.add(RejectedCandidate(mode=mode, symbol=c.perp_symbol, reason=f'perp_short_error: {err[:80]}', funding_rate=c.funding_apr))
                            scan_action = 'perp_short_error'
                            continue

                        # Spot leg → Earn (opt-in). Skipping is fine if no flexible product
                        # exists for the asset; the asset just sits in spot wallet.
                        if not paper and cfg.earn_subscribe_spot_assets:
                            base = c.spot_symbol.split('/')[0]
                            ok_e, err_e = gateway.earn_subscribe_asset(base, qty, False)
                            if ok_e:
                                log_event(db, f'Subscribed {qty:.6f} {base} to flexible Earn', mode=mode)
                            else:
                                log_event(db, f'Earn subscribe for {base} skipped: {err_e[:120]}', mode=mode, level='WARN')

                        # Both legs filled — finalize.
                        pos.perp_entry_price = float(f.get('price') or 0)
                        record_trade(db, pos.id, mode, c.perp_symbol, 'futures', 'sell', qty, f)
                        held_bases.add(base)
                        scan_action = f'opened {c.perp_symbol}'
                        log_event(db, f'Opened {c.perp_symbol} qty={qty:.6f} funding_apy={c.funding_apr:.2%} earn_apr={c.spot_earn_apr:.2%} combined={c.combined_apy:.2%} depth=${c.min_depth_usdt:.0f}', mode=mode)
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
                candidates_total=candidates_total,
                candidates_passing=candidates_passing,
                top_candidates=json.dumps(top_candidates_payload),
                action=scan_action,
                note=scan_note,
            ))

            # If everything is flat in live mode, drain leftover USDT out of the futures
            # wallet back to spot so the Earn sweep below can pick it up. Only safe to
            # drain when there are no open positions (otherwise we'd starve their margin).
            if mode == MODE_LIVE and cfg.auto_transfer_enabled:
                still_open = db.scalar(select(func.count(Position.id)).where(Position.status == 'open', Position.mode == mode)) or 0
                if still_open == 0:
                    bals_after = gateway.safe_balances() or {}
                    fut_free_now = float((bals_after.get('futures', {}).get('USDT') or {}).get('free') or 0)
                    if fut_free_now > 1.0:
                        amt = max(0.0, fut_free_now - 0.10)
                        ok, err = gateway.transfer_futures_to_spot(amt, paper)
                        if ok:
                            log_event(db, f'Auto-transferred {amt:.2f} USDT futures→spot (no open positions)', mode=mode)
                        else:
                            log_event(db, f'futures→spot transfer failed: {err}', mode=mode, level='WARN')

            # Continuous rebalance: keep spot.free ≈ futures.free so both legs of the
            # next arb open are equally funded.
            _rebalance_spot_fut(db, gateway, cfg, mode)

            # Sweep any remaining idle USDT into Earn after entries are done.
            if cfg.earn_enabled:
                _, free_after = _compute_equity_and_free(db, gateway, mode, cfg, earn)
                if free_after > cfg.earn_idle_threshold_usdt:
                    sweep = max(0.0, free_after - 0.10)  # leave a small buffer for rounding/fees
                    ok, err = gateway.earn_subscribe(sweep, paper)
                    if ok:
                        earn.deployed_usdt += sweep
                        log_event(db, f'Swept {sweep:.2f} USDT idle to earn', mode=mode)
                    else:
                        earn.last_error = err
                        log_event(db, f'Earn subscribe failed: {err}', mode=mode, level='WARN')

            prev_snap = db.scalar(select(BalanceSnapshot).where(BalanceSnapshot.source == mode).order_by(BalanceSnapshot.id.desc()).limit(1))
            snap = _take_balance_snapshot(db, gateway, mode, cfg, earn)
            _detect_capital_flow(db, prev_snap, snap, mode)
            db.add(EquityCurve(mode=mode, equity_usdt=snap.total_usdt))
            db.commit()
    except Exception as e:
        with SessionLocal() as db:
            log_event(db, f'Loop iteration error ({mode}): {e}', mode=mode, level='ERROR')
            db.commit()


def run_one_cycle(gateway: BinanceGateway | None = None, mode: str | None = None) -> int:
    """Run one cycle. If `mode` is given, only that mode runs. Otherwise both."""
    if gateway is None:
        gateway = BinanceGateway()
        try:
            gateway.load_markets()
        except Exception as e:
            with SessionLocal() as db:
                log_event(db, f'load_markets failed: {e}', mode=MODE_PAPER, level='ERROR')
                db.commit()
    sleep_seconds = 30
    with SessionLocal() as db:
        cfg = get_strategy_config(db)
        sleep_seconds = max(5, int(cfg.loop_seconds))
    targets = (mode,) if mode else ALL_MODES
    for m in targets:
        run_one_cycle_for_mode(gateway, m)
    return sleep_seconds


def run_loop() -> None:
    gateway = BinanceGateway()
    try:
        gateway.load_markets()
    except Exception as e:
        with SessionLocal() as db:
            log_event(db, f'load_markets failed: {e}', mode=MODE_PAPER, level='ERROR')
            db.commit()
    reconcile_positions(gateway)
    while True:
        sleep_seconds = run_one_cycle(gateway)
        time.sleep(sleep_seconds)
