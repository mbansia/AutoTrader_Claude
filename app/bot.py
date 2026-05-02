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


CAPITAL_FLOW_THRESHOLD_USDT = 50.0

# Module-level dedup so we only log "live API down" once per outage instead of every cycle.
_LIVE_API_UNHEALTHY_LOGGED = False


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


def _force_close_both(db, gateway: BinanceGateway, p: Position, cfg: StrategyConfig, reason: str) -> None:
    paper = (p.mode == MODE_PAPER)
    s = gateway.close_spot(p.spot_symbol, p.quantity, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
    f = gateway.close_perp(p.perp_symbol, p.quantity, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
    record_trade(db, p.id, p.mode, p.spot_symbol, 'spot', 'sell', p.quantity, s)
    record_trade(db, p.id, p.mode, p.perp_symbol, 'futures', 'buy', p.quantity, f)
    p.status = 'closed'
    p.closed_at = datetime.utcnow()
    log_event(db, f'Closed {p.perp_symbol} ({reason}); realized={position_realized_pnl(db, p):+.4f}', mode=p.mode)


def _close_naked_leg(db, gateway: BinanceGateway, p: Position, cfg: StrategyConfig, surviving_leg: str | None, reason: str) -> None:
    paper = (p.mode == MODE_PAPER)
    if surviving_leg == 'spot':
        s = gateway.close_spot(p.spot_symbol, p.quantity, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
        record_trade(db, p.id, p.mode, p.spot_symbol, 'spot', 'sell', p.quantity, s)
    elif surviving_leg == 'perp':
        f = gateway.close_perp(p.perp_symbol, p.quantity, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
        record_trade(db, p.id, p.mode, p.perp_symbol, 'futures', 'buy', p.quantity, f)
    p.status = 'closed'
    p.closed_at = datetime.utcnow()
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
    total_equity = spot_total + fut_total + earn.deployed_usdt
    return total_equity, min(spot_free, fut_free)


def _take_balance_snapshot(db, gateway: BinanceGateway, mode: str, cfg: StrategyConfig, earn: EarnState) -> BalanceSnapshot:
    if mode == MODE_LIVE:
        bals = gateway.safe_balances()
        if bals is not None:
            spot = float((bals['spot'].get('USDT') or {}).get('total') or 0)
            fut = float((bals['futures'].get('USDT') or {}).get('total') or 0)
            snap = BalanceSnapshot(spot_usdt=spot, futures_usdt=fut, total_usdt=spot + fut + earn.deployed_usdt, source=MODE_LIVE)
            db.add(snap)
            db.flush()
            return snap
    total_equity, _ = _compute_equity_and_free(db, gateway, mode, cfg, earn)
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

            # Live short-circuit: if entries are off, no positions, and no earn deployed,
            # skip the API hits entirely. With earn enabled we still want to refresh balances.
            if mode == MODE_LIVE and not mstate.entry_enabled and not mstate.maintenance_mode and not cfg.earn_enabled:
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
                    passing, candidates_total, rejected_scan = gateway.scan_funding(cfg.entry_funding_threshold, cfg.min_24h_quote_volume)
                except Exception as e:
                    passing, candidates_total, rejected_scan = [], 0, []
                    log_event(db, f'scan_funding failed: {e}', mode=mode, level='ERROR')
                candidates_passing = len(passing)
                top_candidates_payload = [{'perp': c.perp_symbol, 'fr': c.funding_rate, 'apr': c.funding_apr, 'interval_h': c.funding_interval_hours, 'qv': c.quote_volume} for c in passing[:5]]
                for sym, reason, apr in rejected_scan[:20]:
                    db.add(RejectedCandidate(mode=mode, symbol=sym, reason=reason, funding_rate=apr))

                total_equity, free = _compute_equity_and_free(db, gateway, mode, cfg, earn)
                if total_equity <= 0:
                    scan_action = 'balances_unavailable'
                    scan_note = 'safe_balances() returned None or paper equity = 0'
                else:
                    scan_action = 'no_fill'
                    desired_notional = cfg.max_position_pct * total_equity
                    min_notional = cfg.min_position_pct * total_equity
                    for c in passing[:5]:
                        if db.scalar(select(Position).where(Position.perp_symbol == c.perp_symbol, Position.status == 'open', Position.mode == mode)):
                            continue
                        # If free is short of what we want, redeem from earn first.
                        if cfg.earn_enabled and free < desired_notional and earn.deployed_usdt > 0:
                            need = min(desired_notional - free, earn.deployed_usdt)
                            ok, err = gateway.earn_redeem(need, paper)
                            if ok:
                                earn.deployed_usdt -= need
                                free += need
                                log_event(db, f'Redeemed {need:.2f} USDT from earn before opening {c.perp_symbol}', mode=mode)
                            else:
                                earn.last_error = err
                                log_event(db, f'Earn redeem failed for {c.perp_symbol}: {err}', mode=mode, level='WARN')
                        sized_notional = min(free * 0.97, desired_notional)
                        if sized_notional < min_notional:
                            db.add(RejectedCandidate(mode=mode, symbol=c.perp_symbol, reason=f'below min position pct ({sized_notional:.2f} < {min_notional:.2f} USDT)', funding_rate=c.funding_apr))
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
                        s = gateway.create_spot_buy(c.spot_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                        f = gateway.create_perp_short(c.perp_symbol, qty, paper, cfg.paper_slippage_bps, cfg.paper_fee_bps)
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
                            perp_entry_price=float(f.get('price') or 0),
                            last_funding_accrual_ts=datetime.utcnow(),
                        )
                        db.add(pos)
                        db.flush()
                        record_trade(db, pos.id, mode, c.spot_symbol, 'spot', 'buy', qty, s)
                        record_trade(db, pos.id, mode, c.perp_symbol, 'futures', 'sell', qty, f)
                        scan_action = f'opened {c.perp_symbol}'
                        log_event(db, f'Opened {c.perp_symbol} qty={qty:.6f} apr={c.funding_apr:.2%}', mode=mode)
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
