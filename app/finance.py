"""Pure-Python financial calculations, free of any HTTP / SQL state.

* PnL
    - :func:`position_realized_pnl` — closed-position trade PnL from the
      Trade ledger (spot sells − spot buys + perp sells − perp buys − fees).
    - :func:`position_unrealized_pnl` — mark-to-market on an open arb.
    - :func:`total_realized_pnl`, :func:`total_funding_income`,
      :func:`net_capital_in` — aggregations over the active mode (and
      optionally a Binance gateway for net-injected capital).

* Annualization
    - :func:`effective_position_apy` — what the trader actually earns on
      total deployed capital, accounting for both legs at the configured
      leverage. ``effective ≈ funding × leverage / (leverage + 1)``.

* Rate of return
    - :func:`xirr` — Newton–Raphson XIRR over (date, cashflow) pairs.
    - :func:`portfolio_xirr` — convenience wrapper that fetches capital
      flows for a mode and adds the current equity as the terminal flow.

* Equity composition
    - :func:`equity_breakdown` — per-bucket slice for the donut chart on
      the dashboard. Buckets differ for paper vs live.
    - :func:`equity_donut_svg` — pure-SVG donut renderer (no JS chart
      dep).
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import select

from app.models import CapitalFlow, Position, Trade


def position_realized_pnl(db, position: Position) -> float:
    """Closed-position trade PnL.

    Computed as the matched-fraction PnL on each leg: only the buy/sell
    pair that actually offset each other counts. Unmatched fills (e.g., a
    spot buy whose corresponding sell was skipped because Binance's
    LOT_SIZE filter made it un-tradeable dust, or a rehydrated orphan perp
    that has no recorded entry) are treated as inventory adjustments —
    they show up in the equity composition (as base-asset balances or
    futures wallet drift) and must not also pollute realized PnL.

    Without this matching, dust-skipped closes inflate realized PnL by
    -spot_buy_notional per position because the entry buy is recorded but
    no sell ever offsets it.
    """
    trades = db.scalars(select(Trade).where(Trade.position_id == position.id)).all()

    spot_buy_qty = sum(t.quantity for t in trades if t.venue == 'spot' and t.side == 'buy')
    spot_buy_val = sum(t.price * t.quantity for t in trades if t.venue == 'spot' and t.side == 'buy')
    spot_sell_qty = sum(t.quantity for t in trades if t.venue == 'spot' and t.side == 'sell')
    spot_sell_val = sum(t.price * t.quantity for t in trades if t.venue == 'spot' and t.side == 'sell')

    perp_open_qty = sum(t.quantity for t in trades if t.venue == 'futures' and t.side == 'sell')
    perp_open_val = sum(t.price * t.quantity for t in trades if t.venue == 'futures' and t.side == 'sell')
    perp_close_qty = sum(t.quantity for t in trades if t.venue == 'futures' and t.side == 'buy')
    perp_close_val = sum(t.price * t.quantity for t in trades if t.venue == 'futures' and t.side == 'buy')

    spot_pnl = 0.0
    if spot_buy_qty > 0 and spot_sell_qty > 0:
        matched = min(spot_buy_qty, spot_sell_qty)
        spot_pnl = (spot_sell_val * matched / spot_sell_qty) - (spot_buy_val * matched / spot_buy_qty)

    perp_pnl = 0.0
    if perp_open_qty > 0 and perp_close_qty > 0:
        matched = min(perp_open_qty, perp_close_qty)
        # Short = entry-sell minus close-buy (gain when close < entry).
        perp_pnl = (perp_open_val * matched / perp_open_qty) - (perp_close_val * matched / perp_close_qty)

    fees = sum(t.fee for t in trades)
    return spot_pnl + perp_pnl - fees


def position_unrealized_pnl(position: Position, spot_now: float, perp_now: float) -> float:
    if position.spot_entry_price <= 0 or position.perp_entry_price <= 0:
        return 0.0
    spot_leg = (spot_now - position.spot_entry_price) * position.quantity
    perp_leg = (position.perp_entry_price - perp_now) * position.quantity
    return spot_leg + perp_leg


def total_realized_pnl(db, mode: str | None = None, exchange: str | None = None) -> float:
    """Closed-position trade PnL only. Does not include funding income — see total_funding_income.

    ``exchange`` (optional) scopes the sum to a single venue. Used by the
    per-venue cycle's equity calc; the dashboard's headline PnL leaves it
    ``None`` to sum across all venues."""
    stmt = select(Position).where(Position.status == 'closed')
    if mode is not None:
        stmt = stmt.where(Position.mode == mode)
    if exchange is not None:
        stmt = stmt.where(Position.exchange == exchange)
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


def total_funding_income(db, mode: str | None = None, status: str | None = None, exchange: str | None = None) -> float:
    """Sum of funding payments (accrued in paper, would-be auto-credited in live).
    Pass status='open' or 'closed' to scope; default is all. ``exchange`` scopes
    to a single venue."""
    from sqlalchemy import func as _func
    stmt = select(_func.coalesce(_func.sum(Position.funding_income_accrued), 0.0))
    if mode is not None:
        stmt = stmt.where(Position.mode == mode)
    if status is not None:
        stmt = stmt.where(Position.status == status)
    if exchange is not None:
        stmt = stmt.where(Position.exchange == exchange)
    return float(db.scalar(stmt) or 0.0)


def equity_breakdown(db, gateways, mode: str, earn_deployed: float) -> list[dict]:
    """Return a per-component slice of total equity for the donut chart.
    Each item: ``{label, value, color, venue}``. ``venue`` tags the bucket to
    the exchange / broker it lives on so the dashboard can group buckets by
    venue and show a "single pool, multiple venues" breakdown.

    ``gateways`` is the list of configured venue gateways. In LIVE mode this
    walks every gateway and contributes its own buckets, so KuCoin balances
    and Binance balances both appear in one pool. In paper mode the breakdown
    is a single virtual book — gateways are unused.

    For backwards compatibility ``gateways`` may be a single gateway object;
    older callers pre-Phase-1 passed one in directly."""
    from app.models import Position, MODE_LIVE
    PALETTE = {
        'spot_usdt': '#38bdf8',
        'fut_usdt': '#fbbf24',
        'spot_assets': '#818cf8',
        'earn': '#4ade80',
        'free_cash': '#38bdf8',
        'in_positions': '#818cf8',
    }
    # Normalise gateways into a list — tolerate the old single-arg signature.
    if not isinstance(gateways, list):
        gateways = [gateways] if gateways is not None else []
    items: list[dict] = []
    if mode == MODE_LIVE:
        for gw in gateways:
            bals = gw.safe_balances() or {}
            spot_usdt = float((bals.get('spot', {}).get('USDT') or {}).get('total') or 0)
            fut_usdt = float((bals.get('futures', {}).get('USDT') or {}).get('total') or 0)
            spot_assets = 0.0
            META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
            for asset, bal in (bals.get('spot') or {}).items():
                if asset in META_KEYS or asset == 'USDT' or not isinstance(bal, dict):
                    continue
                qty = float(bal.get('total') or 0)
                if qty <= 0:
                    continue
                px = gw.safe_price(f'{asset}/USDT') or 0
                spot_assets += qty * px
            # Earn balance is fetched live per venue every render — this is a
            # trading dashboard and stale earn numbers are misleading. Each
            # gateway hits its own earn endpoint (Binance simple-earn,
            # KuCoin funding wallet). One extra SAPI call per venue per
            # render is acceptable; the cost of a stale number is higher.
            try:
                gw_earn, _ = gw.earn_balance_usdt()
                gw_earn = gw_earn or 0.0
            except Exception:
                gw_earn = 0.0
            items.append({'label': f'{gw.name} · Spot USDT', 'value': spot_usdt, 'color': PALETTE['spot_usdt'], 'venue': gw.venue_id})
            items.append({'label': f'{gw.name} · Futures USDT', 'value': fut_usdt, 'color': PALETTE['fut_usdt'], 'venue': gw.venue_id})
            items.append({'label': f'{gw.name} · Spot assets', 'value': spot_assets, 'color': PALETTE['spot_assets'], 'venue': gw.venue_id})
            if gw_earn > 0:
                items.append({'label': f'{gw.name} · Earn', 'value': gw_earn, 'color': PALETTE['earn'], 'venue': gw.venue_id})
    else:
        # Paper mode — use the first gateway (typically Binance) for price lookups.
        price_gw = gateways[0] if gateways else None
        open_ps = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode)).all()
        in_positions = sum(p.quantity * p.spot_entry_price for p in open_ps)
        unrealized = 0.0
        if price_gw is not None:
            for p in open_ps:
                spot_now = price_gw.safe_price(p.spot_symbol) or p.spot_entry_price or 0
                perp_now = price_gw.safe_price(p.perp_symbol, perp=True) or p.perp_entry_price or 0
                unrealized += position_unrealized_pnl(p, spot_now, perp_now)
        # Free cash for paper = total_equity - earn - in_positions - unrealized.
        # Caller computes total_equity separately; we expose the raw components.
        # We don't include realized + funding in items because they're already
        # rolled into "free cash" in paper accounting.
        items.append({'label': 'Paper · In positions', 'value': max(0.0, in_positions), 'color': PALETTE['in_positions'], 'venue': 'paper'})
        items.append({'label': 'Paper · In Earn', 'value': earn_deployed, 'color': PALETTE['earn'], 'venue': 'paper'})
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


def net_capital_in(db, mode: str | None = None, gateways=None) -> tuple[float, dict]:
    """Net capital injected (deposits − withdrawals) across every configured
    venue.

    LIVE mode: walks every gateway's ``net_injected_capital_usdt()``,
    summing the deposit / withdrawal / sub-transfer totals. Each venue's
    history is independent — a deposit into the KuCoin sub-account adds to
    the pool's net injected capital in the same row as a Binance deposit.
    Falls back to manual ``CapitalFlow`` rows for any venue whose history
    isn't yet wired (KuCoin today).

    PAPER mode (or no gateways): just sums the manual ``CapitalFlow`` table.

    Returns ``(value, meta)``. ``meta`` carries 'source' = 'venues' or
    'capital_flows' plus per-venue breakdown counts for the UI.
    """
    from app.models import MODE_LIVE
    # Tolerate the legacy single-gateway signature: a list, a single
    # gateway, or None.
    if gateways is None:
        gw_list: list = []
    elif isinstance(gateways, list):
        gw_list = gateways
    else:
        gw_list = [gateways]

    if mode == MODE_LIVE and gw_list:
        # Sum across venues — any venue that returns None is excluded from
        # the venue total but its manual capital flows still count below.
        venue_total = 0.0
        venue_breakdown: dict[str, dict] = {}
        any_venue_data = False
        for gw in gw_list:
            try:
                value, meta = gw.net_injected_capital_usdt()
            except Exception as e:
                meta = {'error': str(e)[:120]}
                value = None
            if value is not None:
                venue_total += value
                any_venue_data = True
            venue_breakdown[gw.venue_id] = {**(meta or {}), 'value': value}

        # Add manual CapitalFlow rows whose venue didn't return live data.
        # Avoids double-counting a Binance deposit if Binance's API also
        # reported it.
        manual_total = 0.0
        manual_count = 0
        if any_venue_data or True:  # always include manual rows for venues without API history
            stmt = select(CapitalFlow).where(CapitalFlow.mode == mode)
            covered_venues = {vid for vid, m in venue_breakdown.items() if m.get('value') is not None}
            for f in db.scalars(stmt).all():
                if f.exchange in covered_venues:
                    continue  # venue's own API already accounted for it
                manual_total += f.amount_usdt
                manual_count += 1
        meta = {
            'source': 'venues',
            'venue_breakdown': venue_breakdown,
            'manual_count': manual_count,
            'manual_total': manual_total,
        }
        if any_venue_data:
            return venue_total + manual_total, meta
        # No venue had any data — fall through to pure manual.

    stmt = select(CapitalFlow)
    if mode is not None:
        stmt = stmt.where(CapitalFlow.mode == mode)
    flows = db.scalars(stmt).all()
    total = sum(f.amount_usdt for f in flows)
    return total, {'source': 'capital_flows', 'count': len(flows)}


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
    """Annualised return across the capital-flow stream + present equity.

    Deduplication: when a venue has both auto-ingested (API-derived) and
    manual rows, the auto rows are authoritative — manual rows for that
    venue are dropped to avoid double-counting the same deposit. Venues
    without any auto rows fall through to their manual rows.

    Returns ``None`` (rendered "—" in the UI) when XIRR is meaningless:
    too few flows, mixed signs missing, or the holding period is shorter
    than 7 days — the latter cap matters because annualising a tiny gain
    over minutes produces astronomical numbers (the user reported a 30+
    digit XIRR after a same-day deposit). Magnitudes >100 (±10000%) are
    also clamped to ``None`` so the UI never renders junk."""
    now = now or datetime.utcnow()
    stmt = select(CapitalFlow)
    if mode is not None:
        stmt = stmt.where(CapitalFlow.mode == mode)
    flows = db.scalars(stmt).all()
    if not flows:
        return None
    venues_with_auto = {f.exchange for f in flows if f.detected_by == 'auto'}
    flows = [f for f in flows if not (f.detected_by == 'manual' and f.exchange in venues_with_auto)]
    if not flows:
        return None
    earliest = min(f.ts for f in flows)
    if (now - earliest).total_seconds() < 7 * 86400:
        return None
    cf: list[tuple[datetime, float]] = [(f.ts, -f.amount_usdt) for f in flows]
    cf.append((now, current_equity))
    rate = xirr(cf)
    if rate is None or abs(rate) > 100.0:
        return None
    return rate
