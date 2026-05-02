from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.config import (
    DEFAULT_MAINTENANCE_MODE,
    DEFAULT_PAPER_MODE,
    settings,
)
from app.db import SessionLocal
from app.exchange import BinanceGateway, _interval_hours, annualize_rate
from app.finance import position_realized_pnl, position_unrealized_pnl, total_realized_pnl
from app.safety import (
    basis_bps,
    check_hedge,
    check_market_health,
    is_basis_entry_acceptable,
    is_basis_exit_favorable,
)
from app.models import (
    BalanceSnapshot,
    BotEvent,
    CapitalFlow,
    EquityCurve,
    Position,
    RejectedCandidate,
    RuntimeState,
    ScanResult,
    StrategyConfig,
    Trade,
)


CAPITAL_FLOW_THRESHOLD_USDT = 50.0


def log_event(db, message: str, level: str = 'INFO'):
    db.add(BotEvent(level=level, message=message))


def get_runtime_state(db) -> RuntimeState:
    state = db.scalar(select(RuntimeState).where(RuntimeState.id == 1))
    if state is None:
        state = RuntimeState(id=1, paper_mode=DEFAULT_PAPER_MODE, maintenance_mode=DEFAULT_MAINTENANCE_MODE)
        db.add(state)
        db.flush()
    return state


_LEGACY_PERIOD_SENTINEL = 0.005  # any threshold below this is treated as a legacy per-period value


def get_strategy_config(db) -> StrategyConfig:
    cfg = db.scalar(select(StrategyConfig).where(StrategyConfig.id == 1))
    if cfg is None:
        cfg = StrategyConfig(id=1)
        db.add(cfg)
        db.flush()
        return cfg
    # One-time migration: thresholds used to be per-funding-period; now they're APR.
    # Detect legacy values and convert assuming an 8h funding interval (1095 periods/yr).
    migrated = False
    if 0 < cfg.entry_funding_threshold < _LEGACY_PERIOD_SENTINEL:
        cfg.entry_funding_threshold = round(cfg.entry_funding_threshold * 1095.0, 6)
        migrated = True
    if 0 < cfg.exit_funding_threshold < _LEGACY_PERIOD_SENTINEL:
        cfg.exit_funding_threshold = round(cfg.exit_funding_threshold * 1095.0, 6)
        migrated = True
    if migrated:
        log_event(db, f'Migrated funding thresholds to APR: entry={cfg.entry_funding_threshold:.4f}, exit={cfg.exit_funding_threshold:.4f}')
        db.flush()
    return cfg


def record_trade(db, position_id: int | None, symbol: str, venue: str, side: str, qty: float, order: dict):
    db.add(Trade(position_id=position_id, symbol=symbol, venue=venue, side=side, quantity=qty, price=float(order.get('price') or 0), fee=float((order.get('fee') or {}).get('cost') or 0)))


def _force_close_both(db, gateway: BinanceGateway, p: Position, paper_mode: bool, cfg: StrategyConfig, reason: str) -> None:
    """Close both legs of a Position immediately, regardless of basis. Used for stop-loss, delisting,
    maintenance mode, and any other mandatory exit."""
    s = gateway.close_spot(p.spot_symbol, p.quantity, paper_mode, cfg.paper_slippage_bps, cfg.paper_fee_bps)
    f = gateway.close_perp(p.perp_symbol, p.quantity, paper_mode, cfg.paper_slippage_bps, cfg.paper_fee_bps)
    record_trade(db, p.id, p.spot_symbol, 'spot', 'sell', p.quantity, s)
    record_trade(db, p.id, p.perp_symbol, 'futures', 'buy', p.quantity, f)
    p.status = 'closed'
    p.closed_at = datetime.utcnow()
    log_event(db, f'Closed {p.perp_symbol} ({reason}); realized={position_realized_pnl(db, p):+.4f}')


def _close_naked_leg(db, gateway: BinanceGateway, p: Position, paper_mode: bool, cfg: StrategyConfig, surviving_leg: str | None, reason: str) -> None:
    """A leg disappeared from the exchange (liquidation, manual close, withdrawal). Flatten the
    remaining leg so we don't sit on directional exposure, then mark the position closed."""
    if surviving_leg == 'spot':
        s = gateway.close_spot(p.spot_symbol, p.quantity, paper_mode, cfg.paper_slippage_bps, cfg.paper_fee_bps)
        record_trade(db, p.id, p.spot_symbol, 'spot', 'sell', p.quantity, s)
    elif surviving_leg == 'perp':
        f = gateway.close_perp(p.perp_symbol, p.quantity, paper_mode, cfg.paper_slippage_bps, cfg.paper_fee_bps)
        record_trade(db, p.id, p.perp_symbol, 'futures', 'buy', p.quantity, f)
    p.status = 'closed'
    p.closed_at = datetime.utcnow()
    log_event(db, f'Closed naked leg on {p.perp_symbol}: {reason}; flattened {surviving_leg or "no surviving leg"}', level='ERROR')


def reconcile_positions(gateway: BinanceGateway) -> None:
    with SessionLocal() as db:
        for p in gateway.open_perp_positions_raw():
            symbol = p.get('symbol')
            pos_amt = float(p.get('contracts') or p.get('info', {}).get('positionAmt') or 0)
            existing = db.scalar(select(Position).where(Position.perp_symbol == symbol, Position.status == 'open'))
            if existing or pos_amt == 0:
                continue
            base = symbol.split('/')[0]
            db.add(Position(symbol=base, spot_symbol=f'{base}/USDT', perp_symbol=symbol, quantity=abs(pos_amt), entry_funding_rate=0.0))
            log_event(db, f'Rehydrated orphan position {symbol} qty={pos_amt}')
        db.commit()


def _take_balance_snapshot(db, gateway: BinanceGateway, paper_mode: bool, cfg: StrategyConfig) -> BalanceSnapshot:
    if not paper_mode:
        bals = gateway.safe_balances()
        if bals is not None:
            spot = float((bals['spot'].get('USDT') or {}).get('total') or 0)
            fut = float((bals['futures'].get('USDT') or {}).get('total') or 0)
            snap = BalanceSnapshot(spot_usdt=spot, futures_usdt=fut, total_usdt=spot + fut, source='live')
            db.add(snap)
            db.flush()
            return snap
    realized = total_realized_pnl(db)
    open_positions = db.scalars(select(Position).where(Position.status == 'open')).all()
    unrealized = 0.0
    for p in open_positions:
        spot_now = gateway.safe_price(p.spot_symbol) or p.spot_entry_price or 0
        perp_now = gateway.safe_price(p.perp_symbol, perp=True) or p.perp_entry_price or 0
        unrealized += position_unrealized_pnl(p, spot_now, perp_now)
    capital_in = db.scalar(select(func.coalesce(func.sum(CapitalFlow.amount_usdt), 0.0))) or 0.0
    base_equity = (capital_in or 0.0) + cfg.paper_starting_equity
    total = base_equity + realized + unrealized
    snap = BalanceSnapshot(spot_usdt=total, futures_usdt=0.0, total_usdt=total, source='paper')
    db.add(snap)
    db.flush()
    return snap


def _detect_capital_flow(db, prev: BalanceSnapshot | None, curr: BalanceSnapshot) -> None:
    if prev is None or prev.source != curr.source or curr.source == 'paper':
        return
    delta = curr.total_usdt - prev.total_usdt
    if abs(delta) < CAPITAL_FLOW_THRESHOLD_USDT:
        return
    trade_count = db.scalar(select(func.count(Trade.id)).where(Trade.ts > prev.ts, Trade.ts <= curr.ts)) or 0
    if trade_count > 0:
        return
    cf = CapitalFlow(
        amount_usdt=delta,
        kind='deposit' if delta > 0 else 'withdrawal',
        detected_by='auto',
        note=f'Auto-detected balance jump of {delta:+.2f} USDT with no trade activity',
    )
    db.add(cf)
    log_event(db, f'Detected capital flow {delta:+.2f} USDT', level='INFO')


def run_one_cycle(gateway: BinanceGateway | None = None) -> int:
    if gateway is None:
        gateway = BinanceGateway()
        try:
            gateway.load_markets()
        except Exception as e:
            with SessionLocal() as db:
                log_event(db, f'load_markets failed: {e}', level='ERROR')
                db.commit()
    sleep_seconds = 30
    try:
        with SessionLocal() as db:
            state = get_runtime_state(db)
            cfg = get_strategy_config(db)
            sleep_seconds = max(5, int(cfg.loop_seconds))
            paper_mode = bool(state.paper_mode)

            if state.maintenance_mode:
                for p in db.scalars(select(Position).where(Position.status == 'open')).all():
                    _force_close_both(db, gateway, p, paper_mode, cfg, 'maintenance')
                log_event(db, 'Maintenance mode: closed all open positions')
                db.commit()
                return sleep_seconds

            open_positions = db.scalars(select(Position).where(Position.status == 'open')).all()

            # Phase A: per-position safety (hedge + market health). Real account only — paper mode has no
            # authoritative external state to verify against. Failures here override every other decision
            # except maintenance mode and run before voluntary exits so a delisting beats a basis defer.
            if open_positions and not paper_mode:
                for p in list(open_positions):
                    if cfg.delisting_check:
                        healthy, market_reason = check_market_health(gateway, p)
                        if not healthy:
                            _force_close_both(db, gateway, p, paper_mode, cfg, f'market_unhealthy:{market_reason}')
                            continue
                    if cfg.enforce_hedge_check:
                        hedged, hedge_reason, surviving_leg = check_hedge(gateway, p)
                        if not hedged:
                            _close_naked_leg(db, gateway, p, paper_mode, cfg, surviving_leg, hedge_reason)
                open_positions = db.scalars(select(Position).where(Position.status == 'open')).all()

            # Phase B: voluntary exits (funding decay, max hold, stop-loss).
            if cfg.exit_enabled and open_positions:
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
                            log_event(db, f'Exit deferred for {p.perp_symbol} ({exit_reason}): basis={bps:+.1f}bps > max_exit={cfg.max_exit_basis_bps:.1f}bps')
                            continue

                    if exit_reason:
                        _force_close_both(db, gateway, p, paper_mode, cfg, exit_reason)

            open_positions = db.scalars(select(Position).where(Position.status == 'open')).all()
            daily_trades = db.scalar(select(func.count(Trade.id)).where(Trade.ts >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0))) or 0

            scan_action = 'no_scan'
            scan_note = ''
            top_candidates_payload: list[dict] = []
            candidates_total = 0
            candidates_passing = 0

            if cfg.entry_enabled and len(open_positions) < cfg.max_open_positions and daily_trades < cfg.max_trades_per_day:
                try:
                    passing, candidates_total, rejected_scan = gateway.scan_funding(cfg.entry_funding_threshold, cfg.min_24h_quote_volume)
                except Exception as e:
                    passing, candidates_total, rejected_scan = [], 0, []
                    log_event(db, f'scan_funding failed: {e}', level='ERROR')
                candidates_passing = len(passing)
                top_candidates_payload = [{'perp': c.perp_symbol, 'fr': c.funding_rate, 'apr': c.funding_apr, 'interval_h': c.funding_interval_hours, 'qv': c.quote_volume} for c in passing[:5]]
                for sym, reason, apr in rejected_scan[:20]:
                    db.add(RejectedCandidate(symbol=sym, reason=reason, funding_rate=apr))

                bals = gateway.safe_balances() if not paper_mode else None
                if not paper_mode and bals is None:
                    scan_action = 'balances_unavailable'
                    scan_note = 'safe_balances() returned None'
                else:
                    scan_action = 'no_fill'
                    for c in passing[:5]:
                        if db.scalar(select(Position).where(Position.perp_symbol == c.perp_symbol, Position.status == 'open')):
                            continue
                        if paper_mode:
                            cap_in = db.scalar(select(func.coalesce(func.sum(CapitalFlow.amount_usdt), 0.0))) or 0.0
                            free_spot = free_fut = (cap_in + cfg.paper_starting_equity) / 2.0
                        else:
                            free_spot = float((bals['spot'].get('USDT') or {}).get('free') or 0)
                            free_fut = float((bals['futures'].get('USDT') or {}).get('free') or 0)
                        notional = min(free_spot, free_fut) * 0.97
                        if notional < cfg.min_symbol_notional:
                            db.add(RejectedCandidate(symbol=c.perp_symbol, reason='below min notional', funding_rate=c.funding_apr))
                            scan_action = 'below_min_notional'
                            continue
                        spot_px = gateway.safe_price(c.spot_symbol) or 0
                        perp_px = gateway.safe_price(c.perp_symbol, perp=True) or 0
                        if spot_px <= 0 or perp_px <= 0:
                            continue
                        ok, entry_bps = is_basis_entry_acceptable(spot_px, perp_px, cfg.max_entry_basis_bps)
                        if not ok:
                            db.add(RejectedCandidate(symbol=c.perp_symbol, reason=f'basis_too_wide ({entry_bps:+.1f}bps > ±{cfg.max_entry_basis_bps:.1f})', funding_rate=c.funding_apr))
                            scan_action = 'basis_too_wide'
                            continue
                        qty = min(notional, cfg.max_position_notional) / max(0.0001, spot_px)
                        s = gateway.create_spot_buy(c.spot_symbol, qty, paper_mode, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                        f = gateway.create_perp_short(c.perp_symbol, qty, paper_mode, cfg.paper_slippage_bps, cfg.paper_fee_bps)
                        pos = Position(
                            symbol=c.spot_symbol.split('/')[0],
                            spot_symbol=c.spot_symbol,
                            perp_symbol=c.perp_symbol,
                            quantity=qty,
                            entry_funding_rate=c.funding_rate,
                            last_funding_rate=c.funding_rate,
                            funding_interval_hours=c.funding_interval_hours,
                            spot_entry_price=float(s.get('price') or 0),
                            perp_entry_price=float(f.get('price') or 0),
                        )
                        db.add(pos)
                        db.flush()
                        record_trade(db, pos.id, c.spot_symbol, 'spot', 'buy', qty, s)
                        record_trade(db, pos.id, c.perp_symbol, 'futures', 'sell', qty, f)
                        scan_action = f'opened {c.perp_symbol}'
                        log_event(db, f'Opened {c.perp_symbol} qty={qty:.6f} apr={c.funding_apr:.2%}')
                        break
            else:
                if not cfg.entry_enabled:
                    scan_action = 'entry_disabled'
                elif len(open_positions) >= cfg.max_open_positions:
                    scan_action = 'max_positions_reached'
                elif daily_trades >= cfg.max_trades_per_day:
                    scan_action = 'daily_limit_reached'

            db.add(ScanResult(
                candidates_total=candidates_total,
                candidates_passing=candidates_passing,
                top_candidates=json.dumps(top_candidates_payload),
                action=scan_action,
                note=scan_note,
            ))

            prev_snap = db.scalar(select(BalanceSnapshot).order_by(BalanceSnapshot.id.desc()).limit(1))
            snap = _take_balance_snapshot(db, gateway, paper_mode, cfg)
            _detect_capital_flow(db, prev_snap, snap)
            db.add(EquityCurve(equity_usdt=snap.total_usdt))

            db.commit()
    except Exception as e:
        with SessionLocal() as db:
            log_event(db, f'Loop iteration error: {e}', level='ERROR')
            db.commit()
    return sleep_seconds


def run_loop() -> None:
    gateway = BinanceGateway()
    try:
        gateway.load_markets()
    except Exception as e:
        with SessionLocal() as db:
            log_event(db, f'load_markets failed: {e}', level='ERROR')
            db.commit()
    reconcile_positions(gateway)
    while True:
        with SessionLocal() as db:
            state = get_runtime_state(db)
            if state.maintenance_mode:
                run_one_cycle(gateway)
                return
        sleep_seconds = run_one_cycle(gateway)
        time.sleep(sleep_seconds)
