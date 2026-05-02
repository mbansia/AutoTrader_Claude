from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import select

from app.models import CapitalFlow, Position, Trade


def position_realized_pnl(db, position: Position) -> float:
    trades = db.scalars(select(Trade).where(Trade.position_id == position.id)).all()
    spot_buys = sum(t.price * t.quantity for t in trades if t.venue == 'spot' and t.side == 'buy')
    spot_sells = sum(t.price * t.quantity for t in trades if t.venue == 'spot' and t.side == 'sell')
    perp_sells = sum(t.price * t.quantity for t in trades if t.venue == 'futures' and t.side == 'sell')
    perp_buys = sum(t.price * t.quantity for t in trades if t.venue == 'futures' and t.side == 'buy')
    fees = sum(t.fee for t in trades)
    return (spot_sells - spot_buys) + (perp_sells - perp_buys) - fees


def position_unrealized_pnl(position: Position, spot_now: float, perp_now: float) -> float:
    if position.spot_entry_price <= 0 or position.perp_entry_price <= 0:
        return 0.0
    spot_leg = (spot_now - position.spot_entry_price) * position.quantity
    perp_leg = (position.perp_entry_price - perp_now) * position.quantity
    return spot_leg + perp_leg


def total_realized_pnl(db) -> float:
    closed = db.scalars(select(Position).where(Position.status == 'closed')).all()
    return sum(position_realized_pnl(db, p) for p in closed)


def net_capital_in(db) -> float:
    flows = db.scalars(select(CapitalFlow)).all()
    return sum(f.amount_usdt for f in flows)


def xirr(flows: Iterable[tuple[datetime, float]], guess: float = 0.1, max_iter: int = 200, tol: float = 1e-7) -> float | None:
    items = sorted(flows, key=lambda x: x[0])
    if len(items) < 2:
        return None
    has_pos = any(a > 0 for _, a in items)
    has_neg = any(a < 0 for _, a in items)
    if not (has_pos and has_neg):
        return None
    t0 = items[0][0]
    years = [(t - t0).total_seconds() / (365.25 * 86400.0) for t, _ in items]
    amts = [a for _, a in items]

    def npv(r: float) -> float:
        return sum(a / ((1 + r) ** y) for a, y in zip(amts, years))

    def dnpv(r: float) -> float:
        return sum(-a * y / ((1 + r) ** (y + 1)) for a, y in zip(amts, years))

    r = guess
    for _ in range(max_iter):
        try:
            f = npv(r)
            fp = dnpv(r)
        except (OverflowError, ZeroDivisionError):
            return None
        if fp == 0:
            return None
        new_r = r - f / fp
        if new_r <= -0.999:
            new_r = -0.99
        if abs(new_r - r) < tol:
            return new_r
        r = new_r
    return None


def portfolio_xirr(db, current_equity: float, now: datetime | None = None) -> float | None:
    now = now or datetime.utcnow()
    flows = db.scalars(select(CapitalFlow)).all()
    cf: list[tuple[datetime, float]] = []
    for f in flows:
        cf.append((f.ts, -f.amount_usdt))
    cf.append((now, current_equity))
    return xirr(cf)
