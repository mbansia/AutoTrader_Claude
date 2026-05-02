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


def total_realized_pnl(db, mode: str | None = None) -> float:
    """Closed-position trade PnL only. Does not include funding income — see total_funding_income."""
    stmt = select(Position).where(Position.status == 'closed')
    if mode is not None:
        stmt = stmt.where(Position.mode == mode)
    closed = db.scalars(stmt).all()
    return sum(position_realized_pnl(db, p) for p in closed)


def effective_position_apy(funding_apy: float, leverage: int = 1) -> float:
    """Funding is paid on the perp notional only. The arb deploys equal capital
    on the spot leg (which earns nothing). True yield rate on the *total* capital
    deployed (= spot_notional + perp_margin) is funding_apy × perp_notional /
    (spot_notional + perp_margin).

    For 1x cross margin: perp_margin == perp_notional == spot_notional, so
    effective APY = funding_apy / 2. For higher leverage, perp_margin shrinks
    and effective APY approaches funding_apy."""
    if leverage <= 0:
        leverage = 1
    # Both legs measured at the same notional; perp_margin = notional / leverage.
    # capital_deployed = notional + notional/leverage = notional * (1 + 1/leverage)
    # earning_share = notional / capital_deployed = leverage / (leverage + 1)
    return funding_apy * (leverage / (leverage + 1.0))


def total_funding_income(db, mode: str | None = None, status: str | None = None) -> float:
    """Sum of funding payments (accrued in paper, would-be auto-credited in live).
    Pass status='open' or 'closed' to scope; default is all."""
    from sqlalchemy import func as _func
    stmt = select(_func.coalesce(_func.sum(Position.funding_income_accrued), 0.0))
    if mode is not None:
        stmt = stmt.where(Position.mode == mode)
    if status is not None:
        stmt = stmt.where(Position.status == status)
    return float(db.scalar(stmt) or 0.0)


def equity_breakdown(db, gateway, mode: str, earn_deployed: float) -> list[dict]:
    """Return a per-component slice of total equity for the donut chart.
    Each item: {label, value, color}. Designed for both paper and live."""
    from app.models import Position, MODE_LIVE
    PALETTE = {
        'spot_usdt': '#38bdf8',
        'fut_usdt': '#fbbf24',
        'spot_assets': '#818cf8',
        'earn': '#4ade80',
        'free_cash': '#38bdf8',
        'in_positions': '#818cf8',
    }
    items: list[dict] = []
    if mode == MODE_LIVE:
        bals = gateway.safe_balances() or {}
        spot_usdt = float((bals.get('spot', {}).get('USDT') or {}).get('total') or 0)
        fut_usdt = float((bals.get('futures', {}).get('USDT') or {}).get('total') or 0)
        # Walk every non-USDT spot asset; price each.
        spot_assets = 0.0
        META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
        for asset, bal in (bals.get('spot') or {}).items():
            if asset in META_KEYS or asset == 'USDT' or not isinstance(bal, dict):
                continue
            qty = float(bal.get('total') or 0)
            if qty <= 0:
                continue
            px = gateway.safe_price(f'{asset}/USDT') or 0
            spot_assets += qty * px
        items.append({'label': 'Spot USDT', 'value': spot_usdt, 'color': PALETTE['spot_usdt']})
        items.append({'label': 'Futures USDT', 'value': fut_usdt, 'color': PALETTE['fut_usdt']})
        items.append({'label': 'Spot assets', 'value': spot_assets, 'color': PALETTE['spot_assets']})
        items.append({'label': 'In Earn', 'value': earn_deployed, 'color': PALETTE['earn']})
    else:
        open_ps = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode)).all()
        in_positions = sum(p.quantity * p.spot_entry_price for p in open_ps)
        unrealized = 0.0
        for p in open_ps:
            spot_now = gateway.safe_price(p.spot_symbol) or p.spot_entry_price or 0
            perp_now = gateway.safe_price(p.perp_symbol, perp=True) or p.perp_entry_price or 0
            unrealized += position_unrealized_pnl(p, spot_now, perp_now)
        # Free cash for paper = total_equity - earn - in_positions - unrealized.
        # Caller computes total_equity separately; we expose the raw components.
        # We don't include realized + funding in items because they're already
        # rolled into "free cash" in paper accounting.
        items.append({'label': 'In positions', 'value': max(0.0, in_positions), 'color': PALETTE['in_positions']})
        items.append({'label': 'In Earn', 'value': earn_deployed, 'color': PALETTE['earn']})
        # 'Free cash' computed by caller and added so totals match.
    return items


def equity_donut_svg(items: list[dict], cx: int = 100, cy: int = 100, r: int = 80) -> str:
    """Render an SVG donut chart. Returns inner-path markup; caller wraps in <svg>."""
    import math
    total = sum(max(0.0, i['value']) for i in items)
    if total <= 0:
        return ''
    paths: list[str] = []
    angle = -math.pi / 2  # start at 12 o'clock
    for it in items:
        v = max(0.0, it['value'])
        if v <= 0:
            continue
        frac = v / total
        if frac >= 0.999:  # single full slice — render as a circle to avoid path math edge cases
            paths.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{it["color"]}"/>')
            break
        sweep = frac * 2 * math.pi
        x1 = cx + r * math.cos(angle)
        y1 = cy + r * math.sin(angle)
        angle += sweep
        x2 = cx + r * math.cos(angle)
        y2 = cy + r * math.sin(angle)
        large_arc = 1 if sweep > math.pi else 0
        d = f'M {cx},{cy} L {x1:.2f},{y1:.2f} A {r},{r} 0 {large_arc},1 {x2:.2f},{y2:.2f} Z'
        paths.append(f'<path d="{d}" fill="{it["color"]}"/>')
    return ''.join(paths)


def net_capital_in(db, mode: str | None = None) -> float:
    stmt = select(CapitalFlow)
    if mode is not None:
        stmt = stmt.where(CapitalFlow.mode == mode)
    flows = db.scalars(stmt).all()
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


def portfolio_xirr(db, current_equity: float, mode: str | None = None, now: datetime | None = None) -> float | None:
    now = now or datetime.utcnow()
    stmt = select(CapitalFlow)
    if mode is not None:
        stmt = stmt.where(CapitalFlow.mode == mode)
    flows = db.scalars(stmt).all()
    cf: list[tuple[datetime, float]] = []
    for f in flows:
        cf.append((f.ts, -f.amount_usdt))
    cf.append((now, current_equity))
    return xirr(cf)
