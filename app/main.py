from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select

from app.bot import get_runtime_state, get_strategy_config, run_loop, run_one_cycle
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.exchange import BinanceGateway, annualize_rate
from app.finance import (
    net_capital_in,
    portfolio_xirr,
    position_realized_pnl,
    position_unrealized_pnl,
    total_realized_pnl,
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
    Trade,
)

app = FastAPI(title='Funding Arb Bot')
app.mount('/static', StaticFiles(directory='app/static'), name='static')
templates = Jinja2Templates(directory='app/templates')
security = HTTPBasic()


_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def _worker_alive() -> bool:
    return _worker_thread is not None and _worker_thread.is_alive()


def _start_worker() -> bool:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return False
        _worker_thread = threading.Thread(target=run_loop, name='bot-worker', daemon=True)
        _worker_thread.start()
        return True


def auth(creds: HTTPBasicCredentials = Depends(security)):
    if creds.username != settings.dashboard_user or creds.password != settings.dashboard_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={'WWW-Authenticate': 'Basic'})


@app.on_event('startup')
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        get_runtime_state(db)
        get_strategy_config(db)
        db.commit()
    if os.environ.get('BOT_WORKER_ENABLED', '1') not in ('0', 'false', 'False', ''):
        _start_worker()


@app.post('/worker/start')
def worker_start(_: None = Depends(auth)):
    _start_worker()
    return RedirectResponse(url='/dashboard', status_code=303)


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/')
def root():
    return RedirectResponse(url='/dashboard', status_code=303)


def _fmt_ts(dt: datetime | None) -> str:
    if dt is None:
        return '—'
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def _fmt_age(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m'
    if seconds < 86400:
        return f'{seconds // 3600}h{(seconds % 3600) // 60}m'
    return f'{seconds // 86400}d{(seconds % 86400) // 3600}h'


def _current_equity(db) -> tuple[float, str]:
    snap = db.scalar(select(BalanceSnapshot).order_by(desc(BalanceSnapshot.id)).limit(1))
    if snap:
        return snap.total_usdt, f'snapshot {snap.source} @ {_fmt_ts(snap.ts)}'
    eq = db.scalar(select(EquityCurve).order_by(desc(EquityCurve.id)).limit(1))
    if eq:
        return eq.equity_usdt, f'equity_curve @ {_fmt_ts(eq.ts)}'
    return 0.0, 'no data yet'


def _unrealized_for_open(db, gateway: BinanceGateway) -> tuple[float, dict[int, tuple[float, float]]]:
    open_positions = db.scalars(select(Position).where(Position.status == 'open')).all()
    total = 0.0
    prices: dict[int, tuple[float, float]] = {}
    for p in open_positions:
        spot_now = gateway.safe_price(p.spot_symbol) or 0.0
        perp_now = gateway.safe_price(p.perp_symbol, perp=True) or 0.0
        prices[p.id] = (spot_now, perp_now)
        if spot_now and perp_now:
            total += position_unrealized_pnl(p, spot_now, perp_now)
    return total, prices


@app.post('/run-once')
def run_once(_: None = Depends(auth)):
    run_one_cycle()
    return RedirectResponse(url='/dashboard', status_code=303)


@app.post('/mode')
def set_mode(mode: str = Form(...), confirm: str = Form(''), _: None = Depends(auth)):
    with SessionLocal() as db:
        state = get_runtime_state(db)
        if mode == 'live':
            if confirm != 'LIVE':
                raise HTTPException(status_code=400, detail='Type LIVE in confirm box to switch to live mode.')
            state.paper_mode = False
        elif mode == 'paper':
            state.paper_mode = True
        db.commit()
    return RedirectResponse(url='/dashboard', status_code=303)


@app.get('/dashboard', response_class=HTMLResponse)
def dashboard(request: Request, _: None = Depends(auth)):
    with SessionLocal() as db:
        state = get_runtime_state(db)
        cfg = get_strategy_config(db)
        gw = BinanceGateway()

        equity_points = list(reversed(db.scalars(select(EquityCurve).order_by(desc(EquityCurve.id)).limit(60)).all()))
        equity_polyline = ''
        if equity_points:
            xs = list(range(len(equity_points)))
            ys = [pt.equity_usdt for pt in equity_points]
            ymin, ymax = min(ys), max(ys)
            yrange = (ymax - ymin) or 1.0
            xmax = max(1, len(xs) - 1)
            equity_polyline = ' '.join(f'{(x / xmax) * 600:.1f},{110 - (y - ymin) / yrange * 100:.1f}' for x, y in zip(xs, ys))

        latest_scan = db.scalar(select(ScanResult).order_by(desc(ScanResult.id)).limit(1))
        latest_scan_top = []
        if latest_scan:
            try:
                latest_scan_top = json.loads(latest_scan.top_candidates) or []
            except Exception:
                latest_scan_top = []
        # Backfill apr field for old scan rows that pre-date APR storage.
        for c in latest_scan_top:
            if 'apr' not in c:
                c['apr'] = annualize_rate(c.get('fr', 0.0), c.get('interval_h', 8.0))

        current_equity, equity_source = _current_equity(db)
        balances_error = None
        bals = gw.safe_balances() if not state.paper_mode else None
        if not state.paper_mode and bals is None:
            balances_error = 'unable to fetch from Binance'

        realized = total_realized_pnl(db)
        unrealized, _ = _unrealized_for_open(db, gw)
        net_capital = net_capital_in(db)
        flow_count_n = db.scalar(select(func.count(CapitalFlow.id))) or 0
        xirr_value = portfolio_xirr(db, current_equity)
        open_count = db.scalar(select(func.count(Position.id)).where(Position.status == 'open')) or 0
        trades_today_n = db.scalar(select(func.count(Trade.id)).where(Trade.ts >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0))) or 0

        worker_alive = _worker_alive()
        last_cycle_ts = latest_scan.ts if latest_scan else (equity_points[-1].ts if equity_points else None)
        last_cycle_age = _fmt_age(datetime.utcnow() - last_cycle_ts) if last_cycle_ts else None

        ctx = {
            'request': request,
            'active': 'dashboard',
            'state': state,
            'cfg': cfg,
            'current_equity': current_equity,
            'equity_source': equity_source,
            'balances_error': balances_error,
            'realized_pnl': realized,
            'unrealized_pnl': unrealized,
            'total_pnl': realized + unrealized,
            'net_capital': net_capital,
            'flow_count': flow_count_n,
            'xirr_value': xirr_value,
            'open_count': open_count,
            'trades_today': trades_today_n,
            'equity_points': [{'ts': _fmt_ts(p.ts), 'equity_usdt': p.equity_usdt} for p in equity_points],
            'equity_polyline': equity_polyline,
            'latest_scan': {'ts': _fmt_ts(latest_scan.ts), 'candidates_total': latest_scan.candidates_total, 'candidates_passing': latest_scan.candidates_passing, 'action': latest_scan.action} if latest_scan else None,
            'latest_scan_top': latest_scan_top,
            'worker_alive': worker_alive,
            'last_cycle_age': last_cycle_age,
        }
    return templates.TemplateResponse(request, 'dashboard.html', ctx)


@app.get('/positions', response_class=HTMLResponse)
def positions_page(request: Request, _: None = Depends(auth)):
    with SessionLocal() as db:
        state = get_runtime_state(db)
        cfg = get_strategy_config(db)
        gw = BinanceGateway()
        open_positions = db.scalars(select(Position).where(Position.status == 'open')).all()
        rows = []
        for p in open_positions:
            spot_now = gw.safe_price(p.spot_symbol) or 0.0
            perp_now = gw.safe_price(p.perp_symbol, perp=True) or 0.0
            interval_h = p.funding_interval_hours or 8.0
            rows.append({
                'symbol': p.symbol,
                'quantity': p.quantity,
                'spot_entry': p.spot_entry_price,
                'spot_now': spot_now,
                'perp_entry': p.perp_entry_price,
                'perp_now': perp_now,
                'entry_funding_apr': annualize_rate(p.entry_funding_rate, interval_h),
                'last_funding_apr': annualize_rate(p.last_funding_rate, interval_h),
                'interval_hours': interval_h,
                'age': _fmt_age(datetime.utcnow() - p.opened_at),
                'unrealized_pnl': position_unrealized_pnl(p, spot_now, perp_now) if (spot_now and perp_now) else 0.0,
            })

        closed_rows = db.scalars(select(Position).where(Position.status == 'closed').order_by(desc(Position.id)).limit(20)).all()
        closed = [{
            'symbol': c.symbol,
            'quantity': c.quantity,
            'opened_at': _fmt_ts(c.opened_at),
            'closed_at': _fmt_ts(c.closed_at),
            'realized': position_realized_pnl(db, c),
        } for c in closed_rows]

        ctx = {
            'request': request,
            'active': 'positions',
            'state': state,
            'cfg': cfg,
            'rows': rows,
            'closed': closed,
        }
    return templates.TemplateResponse(request, 'positions.html', ctx)


@app.get('/portfolio', response_class=HTMLResponse)
def portfolio_page(request: Request, _: None = Depends(auth)):
    with SessionLocal() as db:
        state = get_runtime_state(db)
        cfg = get_strategy_config(db)
        gw = BinanceGateway()
        current_equity, equity_source = _current_equity(db)
        realized = total_realized_pnl(db)
        unrealized, _ = _unrealized_for_open(db, gw)
        net_capital = net_capital_in(db)
        xirr_value = portfolio_xirr(db, current_equity)
        flows = db.scalars(select(CapitalFlow).order_by(desc(CapitalFlow.id))).all()

        all_positions = db.scalars(select(Position).order_by(desc(Position.id))).all()
        breakdown = []
        for p in all_positions:
            unreal = 0.0
            if p.status == 'open':
                spot_now = gw.safe_price(p.spot_symbol) or 0.0
                perp_now = gw.safe_price(p.perp_symbol, perp=True) or 0.0
                if spot_now and perp_now:
                    unreal = position_unrealized_pnl(p, spot_now, perp_now)
            breakdown.append({
                'symbol': p.symbol,
                'status': p.status,
                'quantity': p.quantity,
                'opened_at': _fmt_ts(p.opened_at),
                'closed_at': _fmt_ts(p.closed_at) if p.closed_at else None,
                'realized': position_realized_pnl(db, p),
                'unrealized': unreal,
            })

        ctx = {
            'request': request,
            'active': 'portfolio',
            'state': state,
            'cfg': cfg,
            'current_equity': current_equity,
            'equity_source': equity_source,
            'realized_pnl': realized,
            'unrealized_pnl': unrealized,
            'total_pnl': realized + unrealized,
            'net_capital': net_capital,
            'xirr_value': xirr_value,
            'flows': [{'id': f.id, 'ts': _fmt_ts(f.ts), 'amount_usdt': f.amount_usdt, 'kind': f.kind, 'detected_by': f.detected_by, 'note': f.note} for f in flows],
            'breakdown': breakdown,
        }
    return templates.TemplateResponse(request, 'portfolio.html', ctx)


@app.post('/capital-flow')
def add_capital_flow(ts: str = Form(...), amount: float = Form(...), note: str = Form(''), _: None = Depends(auth)):
    try:
        ts_dt = datetime.fromisoformat(ts)
    except ValueError:
        raise HTTPException(status_code=400, detail='invalid date')
    with SessionLocal() as db:
        cf = CapitalFlow(ts=ts_dt, amount_usdt=amount, kind='deposit' if amount > 0 else 'withdrawal', detected_by='manual', note=note)
        db.add(cf)
        db.commit()
    return RedirectResponse(url='/portfolio', status_code=303)


@app.post('/capital-flow/delete')
def delete_capital_flow(id: int = Form(...), _: None = Depends(auth)):
    with SessionLocal() as db:
        f = db.get(CapitalFlow, id)
        if f:
            db.delete(f)
            db.commit()
    return RedirectResponse(url='/portfolio', status_code=303)


@app.get('/logs', response_class=HTMLResponse)
def logs_page(request: Request, _: None = Depends(auth)):
    with SessionLocal() as db:
        state = get_runtime_state(db)
        cfg = get_strategy_config(db)
        scans_raw = db.scalars(select(ScanResult).order_by(desc(ScanResult.id)).limit(50)).all()
        scans = []
        for s in scans_raw:
            top_label = ''
            try:
                top = json.loads(s.top_candidates) or []
                if top:
                    apr = top[0].get('apr')
                    if apr is None:
                        apr = annualize_rate(top[0].get('fr', 0.0), top[0].get('interval_h', 8.0))
                    top_label = f"{top[0]['perp']} @ {apr*100:.2f}% APR"
            except Exception:
                pass
            scans.append({'ts': _fmt_ts(s.ts), 'candidates_total': s.candidates_total, 'candidates_passing': s.candidates_passing, 'action': s.action, 'top_candidate_label': top_label, 'note': s.note})

        events = db.scalars(select(BotEvent).order_by(desc(BotEvent.id)).limit(100)).all()
        events_v = [{'ts': _fmt_ts(e.ts), 'level': e.level, 'message': e.message} for e in events]

        rejected = db.scalars(select(RejectedCandidate).order_by(desc(RejectedCandidate.id)).limit(50)).all()
        rejected_v = [{'ts': _fmt_ts(r.ts), 'symbol': r.symbol, 'reason': r.reason, 'funding_rate': r.funding_rate} for r in rejected]

        trades = db.scalars(select(Trade).order_by(desc(Trade.id)).limit(30)).all()
        trades_v = [{'ts': _fmt_ts(t.ts), 'symbol': t.symbol, 'venue': t.venue, 'side': t.side, 'quantity': t.quantity, 'price': t.price, 'fee': t.fee} for t in trades]

        ctx = {'request': request, 'active': 'logs', 'state': state, 'cfg': cfg, 'scans': scans, 'events': events_v, 'rejected': rejected_v, 'trades': trades_v}
    return templates.TemplateResponse(request, 'logs.html', ctx)


@app.get('/config', response_class=HTMLResponse)
def config_page(request: Request, saved: int = 0, _: None = Depends(auth)):
    with SessionLocal() as db:
        state = get_runtime_state(db)
        cfg = get_strategy_config(db)
        ctx = {'request': request, 'active': 'config', 'state': state, 'cfg': cfg, 'saved': bool(saved)}
    return templates.TemplateResponse(request, 'config.html', ctx)


@app.post('/config')
def save_config(
    entry_funding_threshold: float = Form(...),
    exit_funding_threshold: float = Form(...),
    min_24h_quote_volume: float = Form(...),
    stop_loss_pct: float = Form(...),
    max_open_positions: int = Form(...),
    max_trades_per_day: int = Form(...),
    min_symbol_notional: float = Form(...),
    max_position_notional: float = Form(...),
    max_hold_hours: int = Form(...),
    loop_seconds: int = Form(...),
    paper_slippage_bps: float = Form(...),
    paper_fee_bps: float = Form(...),
    paper_starting_equity: float = Form(...),
    entry_enabled: int = Form(...),
    exit_enabled: int = Form(...),
    _: None = Depends(auth),
):
    with SessionLocal() as db:
        cfg = get_strategy_config(db)
        cfg.entry_funding_threshold = entry_funding_threshold
        cfg.exit_funding_threshold = exit_funding_threshold
        cfg.min_24h_quote_volume = min_24h_quote_volume
        cfg.stop_loss_pct = stop_loss_pct
        cfg.max_open_positions = max_open_positions
        cfg.max_trades_per_day = max_trades_per_day
        cfg.min_symbol_notional = min_symbol_notional
        cfg.max_position_notional = max_position_notional
        cfg.max_hold_hours = max_hold_hours
        cfg.loop_seconds = max(5, loop_seconds)
        cfg.paper_slippage_bps = paper_slippage_bps
        cfg.paper_fee_bps = paper_fee_bps
        cfg.paper_starting_equity = paper_starting_equity
        cfg.entry_enabled = bool(entry_enabled)
        cfg.exit_enabled = bool(exit_enabled)
        db.commit()
    return RedirectResponse(url='/config?saved=1', status_code=303)


@app.get('/safety', response_class=HTMLResponse)
def safety_page(request: Request, _: None = Depends(auth)):
    with SessionLocal() as db:
        state = get_runtime_state(db)
        cfg = get_strategy_config(db)
        ctx = {'request': request, 'active': 'safety', 'state': state, 'cfg': cfg}
    return templates.TemplateResponse(request, 'safety.html', ctx)
