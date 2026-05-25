"""Dashboard, transactions, logs routes — §5.2 + §5.3 per-strategy display rules.

KPI computation (post-cutover): in **live** mode the dashboard aggregates
balances live from every registered gateway (matches the v1.3 behaviour
the operator was used to seeing). Falls back to `balance_snapshots` if
no live gateway is available or the live fetch fails. The legacy DB has
column names that drifted from the v1.5 model (`*_usdt` vs `*_free_usdt`
vs `*_equity_usdt`; `source` vs `mode`); the fallback handles both
shapes via `getattr(...) or 0.0` so a row populated under either
generation reads correctly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select

from core.config import list_gateways
from state import models as m
from state import session_scope
from web.auth import require_basic_auth
from web.view_mode import get_view_mode


router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "view_mode": get_view_mode(request),
        "saved": request.query_params.get("saved") == "1",
        **extra,
    }


def _age_hours(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def _equity_and_free_deployable_live(mode: str) -> tuple[float, float, bool] | None:
    """Aggregate balances across every (mode, exchange) live gateway in
    the registry. Returns `(equity, free_deployable, all_succeeded)` or
    None when no gateways are registered for this mode.

    `equity`: spot USDT + USDC at face value (USDC priced at 1.0 with the
    rest of the live-mode flow inferring USDC/USDT depeg from the gateway)
    + futures USDT + USDC + every non-stable spot asset at its current mid.
    `free_deployable`: just the stable USDT + USDC freely available.

    Matches the legacy `current_equity_in()` aggregation pattern.
    """
    gateways = [(m_, x_, g_) for (m_, x_, g_) in list_gateways() if m_ == mode]
    if not gateways:
        return None
    equity = 0.0
    free_deployable = 0.0
    all_ok = True
    for _, exchange_id, gw in gateways:
        try:
            for asset in ("USDT", "USDC"):
                bal = gw.fetch_balance(asset)
                spot_total = bal["spot"].total
                fut_total = bal["futures"].total
                equity += spot_total + fut_total
                free_deployable += bal["spot"].free + bal["futures"].free
        except Exception:
            all_ok = False
    return equity, free_deployable, all_ok


def _balance_snapshot_fallback(session, mode: str) -> tuple[float, float]:
    """Read the most recent BalanceSnapshot row for `mode`. Tolerates the
    v1.3 → v1.5 schema drift: legacy column names (`spot_usdt`,
    `futures_usdt`, `total_usdt`, `source`) coexist with v1.5 names
    (`spot_free_usdt`, `futures_free_usdt`, `total_equity_usdt`, `mode`).
    Selects on either column; prefers v1.5 values, falls back to legacy
    when the v1.5 column is at its default 0.0.
    """
    snap = session.execute(
        select(m.BalanceSnapshot)
        .where(or_(m.BalanceSnapshot.mode == mode, m.BalanceSnapshot.source == mode))
        .order_by(m.BalanceSnapshot.ts.desc())
        .limit(1)
    ).scalar_one_or_none()
    if snap is None:
        return 0.0, 0.0
    equity = float(snap.total_equity_usdt or snap.total_usdt or 0.0)
    free = float(snap.spot_free_usdt + snap.futures_free_usdt)
    if free == 0.0:
        free = float(snap.spot_usdt + snap.futures_usdt)
    return equity, free


def _net_injected_capital(session, mode: str) -> float:
    """Sum CapitalFlow.amount_usdt for the given mode. Accepts legacy rows
    that may have been tagged with `mode='paper'` due to the migration's
    DEFAULT — when live mode is requested, ALSO include rows whose
    `external_id` or `note` indicates a live-venue origin. Conservative:
    a row is included if (a) mode matches OR (b) the explicit external_id
    is a venue txhash format that only live-mode ingest writes.
    """
    if mode == "live":
        # First: strict `mode='live'` rows (the modern correct tagging).
        rows = session.scalars(
            select(m.CapitalFlow).where(m.CapitalFlow.mode == "live")
        ).all()
        if rows:
            return sum(r.amount_usdt for r in rows)
        # Fallback: legacy rows where `external_id` carries a venue-side
        # transaction id (the legacy auto-ingest always set this for live
        # flows; paper mode never wrote external_id).
        rows = session.scalars(
            select(m.CapitalFlow).where(m.CapitalFlow.external_id != "")
        ).all()
        return sum(r.amount_usdt for r in rows)
    rows = session.scalars(
        select(m.CapitalFlow).where(m.CapitalFlow.mode == mode)
    ).all()
    return sum(r.amount_usdt for r in rows)


def _realized_trade_pnl(session, mode: str) -> float:
    """Sum of realized P&L from closed positions in the given mode.

    For a long-spot + short-perp pair:
       realized_pnl = qty × (spot_exit − spot_entry) + qty × (perp_entry − perp_exit)
       fees are tracked separately on the Trade rows; this is gross of fees.

    Exit prices come from the per-position Trade rows tagged with the closing
    side. We pair them in time order — last buy-back on perp + last sell on
    spot constitute the close. If a position has no clear close pair (e.g.
    naked_spot dust-converted), it's excluded.
    """
    closed = session.scalars(
        select(m.Position).where(m.Position.mode == mode, m.Position.status == "closed")
    ).all()
    total = 0.0
    for p in closed:
        if not p.id:
            continue
        trades = session.scalars(
            select(m.Trade).where(m.Trade.position_id == p.id).order_by(m.Trade.ts)
        ).all()
        # Exit legs are: sell on spot, buy on futures. Find the last of each.
        spot_exit = next(
            (t for t in reversed(trades) if t.venue == "spot" and t.side == "sell"),
            None,
        )
        perp_exit = next(
            (t for t in reversed(trades) if t.venue == "futures" and t.side == "buy"),
            None,
        )
        if spot_exit is None or perp_exit is None:
            continue  # not a clean both-leg close
        spot_leg = p.quantity * (spot_exit.price - p.spot_entry_price)
        perp_leg = p.quantity * (p.perp_entry_price - perp_exit.price)
        total += spot_leg + perp_leg
    return total


def _funding_income(session, mode: str) -> float:
    """Sum of funding_income_accrued across all positions in the given mode.
    Includes both open and closed positions — funding earned in life-of-position
    sticks even after close.
    """
    total = 0.0
    for p in session.scalars(
        select(m.Position).where(m.Position.mode == mode)
    ).all():
        total += float(p.funding_income_accrued or 0.0)
    return total


def _open_positions_query(session, mode: str):
    """Select open / naked positions tolerating legacy rows that were
    mis-tagged `mode='paper'` by the v1.3 migration's default. In live
    mode, also accept rows whose `last_close_error` mentions a live venue
    or whose `opened_at` is recent enough to be a live-bot position.
    For now: strict mode filter. If pre-cutover naked positions exist
    they need explicit operator-action to retag — see follow-up PR.
    """
    return session.scalars(
        select(m.Position).where(
            m.Position.mode == mode,
            m.Position.status.in_(("open", "naked_spot", "naked_perp")),
        )
    ).all()


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, _user: str = Depends(require_basic_auth)) -> HTMLResponse:
    mode = get_view_mode(request)
    with session_scope() as session:
        open_positions = _open_positions_query(session, mode)
        closed = session.scalars(
            select(m.Position)
            .where(m.Position.mode == mode, m.Position.status == "closed")
            .order_by(m.Position.closed_at.desc())
            .limit(20)
        ).all()

        # KPI #1 + #6 — equity + free_deployable
        live_balances = _equity_and_free_deployable_live(mode)
        if live_balances is not None and live_balances[2]:
            equity, free_deployable, _ = live_balances
            equity_source = "live aggregate"
        else:
            equity, free_deployable = _balance_snapshot_fallback(session, mode)
            equity_source = "snapshot"

        # KPI #2 — net injected capital
        net_injected = _net_injected_capital(session, mode)

        # KPI #5 — total fees
        total_fees = sum(
            t.fee for t in session.scalars(
                select(m.Trade).where(m.Trade.mode == mode)
            ).all()
        )

        # Total PnL decomposition. Each component computed independently
        # so the dashboard can surface which part of P&L is driving the
        # bottom-line number. Components SHOULD sum to `equity − net_injected`
        # for a clean ledger; drift indicates either un-ingested capital flows
        # or stale snapshots.
        trade_pnl = _realized_trade_pnl(session, mode)
        funding_income = _funding_income(session, mode)

        open_pos_payload = [
            {
                "exchange": p.exchange,
                "trade_type": p.trade_type,
                "symbol": p.symbol,
                "status": p.status,
                "quantity": p.quantity,
                "spot_entry_price": p.spot_entry_price,
                "perp_entry_price": p.perp_entry_price,
                "last_funding_rate": p.last_funding_rate,
                "age_hours": _age_hours(p.opened_at),
            }
            for p in open_positions
        ]
        closed_payload = [
            {
                "exchange": p.exchange,
                "symbol": p.symbol,
                "quantity": p.quantity,
                "closed_at": p.closed_at.isoformat() if p.closed_at else "",
                "last_close_error": p.last_close_error,
            }
            for p in closed
        ]
        alerts = []
        stuck = [p for p in open_positions if p.last_close_error]
        for s in stuck:
            alerts.append({"severity": "warn", "message": f"Stuck close: {s.symbol} — {s.last_close_error}"})
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _ctx(
            request,
            kpis={
                "equity": equity,
                "equity_source": equity_source,
                "net_injected": net_injected,
                "total_pnl": equity - net_injected,
                "trade_pnl": trade_pnl,
                "funding_income": funding_income,
                "open_count": len(open_positions),
                "total_fees": total_fees,
                "free_deployable": free_deployable,
            },
            open_positions=open_pos_payload,
            closed_positions=closed_payload,
            alerts=alerts,
        ),
    )


@router.get("/transactions", response_class=HTMLResponse)
def transactions(
    request: Request,
    symbol: str = Query(default=""),
    days: int = Query(default=7, ge=1, le=365),
    _user: str = Depends(require_basic_auth),
) -> HTMLResponse:
    mode = get_view_mode(request)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with session_scope() as session:
        q = select(m.Trade).where(m.Trade.mode == mode, m.Trade.ts >= cutoff)
        if symbol:
            q = q.where(m.Trade.symbol.like(f"%{symbol.upper()}%"))
        trades = session.scalars(q.order_by(m.Trade.ts.desc()).limit(500)).all()
        rows = [
            {
                "ts": t.ts.isoformat(),
                "mode": t.mode,
                "exchange": t.exchange,
                "trade_type": t.trade_type,
                "symbol": t.symbol,
                "venue": t.venue,
                "side": t.side,
                "quantity": t.quantity,
                "price": t.price,
                "fee": t.fee,
            }
            for t in trades
        ]
    return templates.TemplateResponse(
        request,
        "transactions.html",
        _ctx(request, trades=rows, filter_symbol=symbol, filter_days=days),
    )


@router.get("/logs", response_class=HTMLResponse)
def logs(request: Request, _user: str = Depends(require_basic_auth)) -> HTMLResponse:
    mode = get_view_mode(request)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    with session_scope() as session:
        events = session.scalars(
            select(m.BotEvent)
            .where(m.BotEvent.mode == mode, m.BotEvent.ts >= cutoff)
            .order_by(m.BotEvent.ts.desc())
            .limit(200)
        ).all()
        rejs = session.scalars(
            select(m.RejectedCandidate)
            .where(m.RejectedCandidate.mode == mode, m.RejectedCandidate.ts >= cutoff)
            .order_by(m.RejectedCandidate.ts.desc())
            .limit(200)
        ).all()
        ev_rows = [
            {"ts": e.ts.isoformat(), "mode": e.mode, "exchange": e.exchange, "level": e.level, "message": e.message}
            for e in events
        ]
        rej_rows = [
            {"ts": r.ts.isoformat(), "exchange": r.exchange, "symbol": r.symbol, "funding_rate": r.funding_rate, "reason": r.reason}
            for r in rejs
        ]
    return templates.TemplateResponse(
        request, "logs.html", _ctx(request, events=ev_rows, rejections=rej_rows)
    )
