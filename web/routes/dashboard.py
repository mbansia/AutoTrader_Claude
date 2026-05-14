"""Dashboard, transactions, logs routes — §5.2 + §5.3 per-strategy display rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

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


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, _user: str = Depends(require_basic_auth)) -> HTMLResponse:
    mode = get_view_mode(request)
    with session_scope() as session:
        open_positions = session.scalars(
            select(m.Position).where(
                m.Position.mode == mode, m.Position.status.in_(("open", "naked_spot", "naked_perp"))
            )
        ).all()
        closed = session.scalars(
            select(m.Position)
            .where(m.Position.mode == mode, m.Position.status == "closed")
            .order_by(m.Position.closed_at.desc())
            .limit(20)
        ).all()
        # KPI computation: equity = idle balances + position-value. Real
        # values come from the live gateway; without a registered one we
        # fall back to balance_snapshots' most-recent row.
        last_snapshot = session.scalars(
            select(m.BalanceSnapshot)
            .where(m.BalanceSnapshot.mode == mode)
            .order_by(m.BalanceSnapshot.ts.desc())
            .limit(1)
        ).one_or_none()
        equity = last_snapshot.total_equity_usdt if last_snapshot else 0.0
        free_deployable = (
            last_snapshot.spot_free_usdt + last_snapshot.futures_free_usdt
        ) if last_snapshot else 0.0
        # Net injected capital = sum of capital_flows.
        capital_rows = session.scalars(
            select(m.CapitalFlow).where(m.CapitalFlow.mode == mode)
        ).all()
        net_injected = sum(r.amount_usdt for r in capital_rows)
        total_fees = sum(t.fee for t in session.scalars(
            select(m.Trade).where(m.Trade.mode == mode)
        ).all())
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
                "net_injected": net_injected,
                "total_pnl": equity - net_injected,
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
