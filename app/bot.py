from __future__ import annotations

import time
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.config import (
    DEFAULT_MAINTENANCE_MODE,
    DEFAULT_PAPER_MODE,
    EXIT_FUNDING_THRESHOLD,
    LOOP_SECONDS,
    MAX_HOLD_HOURS,
    MAX_OPEN_POSITIONS,
    MAX_POSITION_NOTIONAL,
    MAX_TRADES_PER_DAY,
    MIN_SYMBOL_NOTIONAL,
    STOP_LOSS_PCT,
    settings,
)
from app.db import SessionLocal
from app.exchange import BinanceGateway
from app.models import BotEvent, EquityCurve, Position, RejectedCandidate, RuntimeState, Trade


def log_event(db, message: str, level: str = 'INFO'):
    db.add(BotEvent(level=level, message=message))


def get_runtime_state(db) -> RuntimeState:
    state = db.scalar(select(RuntimeState).where(RuntimeState.id == 1))
    if state is None:
        state = RuntimeState(id=1, paper_mode=DEFAULT_PAPER_MODE, maintenance_mode=DEFAULT_MAINTENANCE_MODE)
        db.add(state)
        db.flush()
    return state


def record_trade(db, position_id: int | None, symbol: str, venue: str, side: str, qty: float, order: dict):
    db.add(Trade(position_id=position_id, symbol=symbol, venue=venue, side=side, quantity=qty, price=float(order.get('price') or 0), fee=float((order.get('fee') or {}).get('cost') or 0)))


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


def run_loop() -> None:
    gateway = BinanceGateway()
    gateway.load_markets()
    reconcile_positions(gateway)

    while True:
        with SessionLocal() as db:
            state = get_runtime_state(db)
            paper_mode = bool(state.paper_mode)
            if state.maintenance_mode:
                for p in db.scalars(select(Position).where(Position.status == 'open')).all():
                    s = gateway.close_spot(p.spot_symbol, p.quantity, paper_mode)
                    f = gateway.close_perp(p.perp_symbol, p.quantity, paper_mode)
                    record_trade(db, p.id, p.spot_symbol, 'spot', 'sell', p.quantity, s)
                    record_trade(db, p.id, p.perp_symbol, 'futures', 'buy', p.quantity, f)
                    p.status = 'closed'
                    p.closed_at = datetime.utcnow()
                db.commit()
                return

            open_positions = db.scalars(select(Position).where(Position.status == 'open')).all()
            current_funding = gateway.futures.fetch_funding_rates() if open_positions else {}
            for p in open_positions:
                row = current_funding.get(p.perp_symbol) or {}
                fr = row.get('fundingRate')
                if fr is not None:
                    p.last_funding_rate = float(fr)
                age = datetime.utcnow() - p.opened_at
                exit_reason = None
                if p.last_funding_rate < EXIT_FUNDING_THRESHOLD:
                    exit_reason = 'funding_below_threshold'
                elif age > timedelta(hours=MAX_HOLD_HOURS):
                    exit_reason = 'max_hold'
                elif p.spot_entry_price > 0 and p.perp_entry_price > 0:
                    spot_now = gateway.price(p.spot_symbol)
                    perp_now = gateway.perp_price(p.perp_symbol)
                    pnl = (spot_now - p.spot_entry_price) * p.quantity + (p.perp_entry_price - perp_now) * p.quantity
                    notional = p.spot_entry_price * p.quantity
                    if notional > 0 and pnl / notional <= STOP_LOSS_PCT:
                        exit_reason = 'stop_loss'
                if exit_reason:
                    s = gateway.close_spot(p.spot_symbol, p.quantity, paper_mode)
                    f = gateway.close_perp(p.perp_symbol, p.quantity, paper_mode)
                    record_trade(db, p.id, p.spot_symbol, 'spot', 'sell', p.quantity, s)
                    record_trade(db, p.id, p.perp_symbol, 'futures', 'buy', p.quantity, f)
                    p.status = 'closed'
                    p.closed_at = datetime.utcnow()
                    log_event(db, f'Closed {p.perp_symbol} ({exit_reason})')

            daily_trades = db.scalar(select(func.count(Trade.id)).where(Trade.ts >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0))) or 0
            if len(open_positions) < MAX_OPEN_POSITIONS and daily_trades < MAX_TRADES_PER_DAY:
                for c in gateway.top_funding_candidates()[:5]:
                    if db.scalar(select(Position).where(Position.perp_symbol == c.perp_symbol, Position.status == 'open')):
                        continue
                    bals = gateway.balances()
                    free_spot = float((bals['spot'].get('USDT') or {}).get('free') or 0)
                    free_fut = float((bals['futures'].get('USDT') or {}).get('free') or 0)
                    notional = min(free_spot, free_fut) * 0.97
                    if notional < MIN_SYMBOL_NOTIONAL:
                        db.add(RejectedCandidate(symbol=c.perp_symbol, reason='below min notional', funding_rate=c.funding_rate))
                        continue
                    qty = min(notional, MAX_POSITION_NOTIONAL) / max(0.0001, gateway.price(c.spot_symbol))
                    s = gateway.create_spot_buy(c.spot_symbol, qty, paper_mode)
                    f = gateway.create_perp_short(c.perp_symbol, qty, paper_mode)
                    pos = Position(symbol=c.spot_symbol.split('/')[0], spot_symbol=c.spot_symbol, perp_symbol=c.perp_symbol, quantity=qty, entry_funding_rate=c.funding_rate, last_funding_rate=c.funding_rate, spot_entry_price=float(s.get('price') or 0), perp_entry_price=float(f.get('price') or 0))
                    db.add(pos)
                    db.flush()
                    record_trade(db, pos.id, c.spot_symbol, 'spot', 'buy', qty, s)
                    record_trade(db, pos.id, c.perp_symbol, 'futures', 'sell', qty, f)
                    break

            bals = gateway.balances()
            equity = float((bals['spot'].get('USDT') or {}).get('total') or 0) + float((bals['futures'].get('USDT') or {}).get('total') or 0)
            db.add(EquityCurve(equity_usdt=equity))
            db.commit()
        time.sleep(LOOP_SECONDS)
