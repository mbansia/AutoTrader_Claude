"""FastAPI application — HTTP API + dashboard UI.

This module owns the HTTP surface of the bot:

* App lifecycle: :func:`startup` runs on app start, creates DB
  tables, applies in-place schema migrations, and (unless the env
  var disables it) spawns the bot worker thread.
* Auth: HTTP Basic via :func:`auth`, scoped to every UI route. The
  ``/health`` endpoint and ``/static`` are unauthenticated.
* View toggle: ``/view/{mode}`` sets a cookie that scopes every
  page to either paper or live data.
* Mode controls: ``/mode/{mode}/{stop|start|exit-all-stop}`` flip
  the per-mode :class:`ModeState` flags.
* Position actions: ``/positions/{id}/close`` (manual close),
  ``/run-once`` (synchronous one-cycle nudge), ``/worker/start``.
* Pages: ``/dashboard`` (merged dashboard + positions + portfolio),
  ``/transactions``, ``/logs``, ``/config``, ``/safety``,
  ``/monitoring``.

Worker thread management:

* :data:`_worker_thread` is a module-level :class:`threading.Thread`
  guarded by :data:`_worker_lock`. Single instance per process.
  Started on app startup (gated by ``BOT_WORKER_ENABLED``) or by
  a manual ``POST /worker/start``.

The route bodies are deliberately not split into separate router
modules yet — the codebase is small enough that one file per
concern is overkill. If/when this file passes ~1500 lines,
splitting routes into ``app/routes/*.py`` is the obvious next step.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Path, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import desc, func, select

from app.bot import (
    _position_leg_states,
    get_mode_state,
    get_runtime_state,
    get_strategy_config,
    manual_close,
    run_loop,
    run_one_cycle,
)
from app.config import settings
from app.db import Base, SessionLocal, engine, run_schema_migrations
from app.exchange import BinanceGateway, KuCoinGateway, annualize_rate, make_gateways
from app.finance import (
    effective_position_apy,
    equity_breakdown,
    equity_donut_svg,
    net_capital_in,
    portfolio_xirr,
    position_realized_pnl,
    position_unrealized_pnl,
    total_funding_income,
    total_realized_pnl,
)
from app.models import (
    ALL_MODES,
    MODE_LIVE,
    MODE_PAPER,
    OPEN_STATUSES,
    BalanceSnapshot,
    BotEvent,
    CapitalFlow,
    EquityCurve,
    ModeState,
    Position,
    RejectedCandidate,
    RuntimeState,
    ScanResult,
    TRADE_TYPE_LABELS,
    Trade,
)
from app.network import get_outbound_ip
from app.safety import basis_bps

app = FastAPI(title='Funding Arb Bot')
app.mount('/static', StaticFiles(directory='app/static'), name='static')
templates = Jinja2Templates(directory='app/templates')

# Cache-bust static assets via ?v=<mtime-hash>. Recomputed on each call
# so iterating in dev (uvicorn auto-reload) picks up changes without
# manual ctrl-shift-R; in prod the values are stable per process.
def _static_version() -> str:
    import hashlib
    h = hashlib.sha1()
    for fname in ('style.css', 'tables.js', 'favicon.svg'):
        try:
            h.update(str(os.path.getmtime(f'app/static/{fname}')).encode())
        except OSError:
            pass
    return h.hexdigest()[:8]


templates.env.globals['static_v'] = _static_version()
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


def _resolve_view(view_qs: str | None, view_cookie: str | None) -> str:
    candidate = (view_qs or view_cookie or MODE_PAPER).lower()
    return candidate if candidate in ALL_MODES else MODE_PAPER


def _gateway_for(venue_id: str):
    """Return the configured gateway whose ``venue_id`` matches, or a fresh
    BinanceGateway as a safe default. Used when an action targets a single
    Position/Trade and the route needs the right venue's API client."""
    for gw in make_gateways():
        if gw.venue_id == venue_id:
            return gw
    if venue_id == 'kucoin':
        return KuCoinGateway()
    return BinanceGateway()


@app.on_event('startup')
def startup() -> None:
    # Loud warning if the dashboard is running with the default password.
    # Audit pass: this should fire BEFORE the worker thread spins up so
    # operators see it on first deploy without scrolling past startup.
    if settings.dashboard_password in ('change-me', 'changeme', '', 'admin', 'password'):
        import logging as _lg
        _lg.getLogger('uvicorn.error').warning(
            'SECURITY: dashboard is running with the default/weak password (%r). '
            'Set DASHBOARD_PASSWORD env var to a strong unique value before exposing this service.',
            settings.dashboard_password,
        )
    # Migrations first (drops/widens columns on existing tables), then
    # create_all to recreate any tables migrations dropped and to add new
    # tables introduced since the last deploy.
    run_schema_migrations()
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        get_runtime_state(db)
        get_strategy_config(db)
        for m in ALL_MODES:
            get_mode_state(db, m)
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


def _check_diagnostics_token(token: str | None) -> None:
    """Constant-time-ish check that the caller passed the expected token.
    Refuses every request when DIAGNOSTICS_TOKEN env var is unset so the
    endpoint can't be silently public."""
    expected = settings.diagnostics_token or ''
    if not expected:
        raise HTTPException(status_code=503, detail='diagnostics disabled — set DIAGNOSTICS_TOKEN env var to enable')
    if not token or token != expected:
        raise HTTPException(status_code=401, detail='invalid or missing diagnostics token')


@app.get('/api/diagnostics')
def api_diagnostics(token: str | None = None, hours: int = 24):
    """Structured JSON health snapshot for external monitoring / scheduled
    diagnostics. Pure read; no side effects. Auth via `?token=...` against
    settings.diagnostics_token (separate from dashboard credentials so it
    can be rotated independently).

    The payload is grouped so a downstream rule engine can quickly look
    for known anomaly patterns:
        - `cycle_health`: heartbeat, recent errors/warns count
        - `positions`: per-status counts + per-naked-position age
        - `wallets`: per-venue per-asset per-wallet-type breakdown
        - `rejections_grouped`: last-N-hours reject reasons by category
        - `recent_events`: last 50 WARN/ERROR log entries
        - `anomalies`: rule-based flags ready for human or LLM review

    Caller passes `hours` to widen / narrow the recent-history windows
    (default 24h, max 168h)."""
    _check_diagnostics_token(token)
    hours = max(1, min(int(hours or 24), 168))
    since = datetime.utcnow() - timedelta(hours=hours)
    out: dict = {
        'generated_at_utc': datetime.utcnow().isoformat() + 'Z',
        'window_hours': hours,
    }
    with SessionLocal() as db:
        # ─── Cycle heartbeat ──────────────────────────────────────────
        last_event = db.scalar(select(BotEvent).order_by(desc(BotEvent.id)).limit(1))
        recent_errors = db.scalars(select(BotEvent).where(BotEvent.ts >= since, BotEvent.level == 'ERROR').order_by(desc(BotEvent.id))).all()
        recent_warns = db.scalars(select(BotEvent).where(BotEvent.ts >= since, BotEvent.level == 'WARN').order_by(desc(BotEvent.id))).all()
        out['cycle_health'] = {
            'last_event_ts': last_event.ts.isoformat() + 'Z' if last_event else None,
            'last_event_msg': (last_event.message[:240] if last_event else None),
            'seconds_since_last_event': (datetime.utcnow() - last_event.ts).total_seconds() if last_event else None,
            'error_count': len(recent_errors),
            'warn_count': len(recent_warns),
        }
        # ─── Positions by status + age ────────────────────────────────
        pos_rows = db.scalars(select(Position)).all()
        by_status: dict[str, int] = {}
        naked_positions: list[dict] = []
        open_positions: list[dict] = []
        for p in pos_rows:
            by_status[p.status] = by_status.get(p.status, 0) + 1
            if p.status == 'naked_spot':
                age_min = (datetime.utcnow() - p.opened_at).total_seconds() / 60.0
                naked_positions.append({
                    'id': p.id,
                    'mode': p.mode,
                    'exchange': p.exchange,
                    'symbol': p.symbol,
                    'spot_symbol': p.spot_symbol,
                    'quantity': float(p.quantity or 0),
                    'spot_entry_price': float(p.spot_entry_price or 0),
                    'notional_est': float((p.quantity or 0) * (p.spot_entry_price or 0)),
                    'age_minutes': round(age_min, 1),
                })
            elif p.status == 'open':
                age_h = (datetime.utcnow() - p.opened_at).total_seconds() / 3600.0
                open_positions.append({
                    'id': p.id,
                    'mode': p.mode,
                    'exchange': p.exchange,
                    'symbol': p.symbol,
                    'perp_symbol': p.perp_symbol,
                    'quote_currency': p.quote_currency,
                    'quantity': float(p.quantity or 0),
                    'spot_entry_price': float(p.spot_entry_price or 0),
                    'perp_entry_price': float(p.perp_entry_price or 0),
                    'last_funding_rate': float(p.last_funding_rate or 0),
                    'funding_interval_h': float(p.funding_interval_hours or 8),
                    'last_close_error': p.last_close_error or '',
                    'age_hours': round(age_h, 2),
                })
        out['positions'] = {
            'by_status': by_status,
            'open': open_positions,
            'naked': naked_positions,
        }
        # ─── Wallets per venue ────────────────────────────────────────
        wallets: dict = {}
        for gw in make_gateways():
            try:
                wb = gw.wallet_breakdown()
                wallets[gw.venue_id] = wb
            except Exception as e:
                wallets[gw.venue_id] = {'error': str(e)[:160]}
        out['wallets'] = wallets
        # ─── Rejections grouped by reason category (last `hours`) ─────
        recent_rejects = db.scalars(select(RejectedCandidate).where(RejectedCandidate.ts >= since)).all()
        grouped: dict[str, dict[str, int]] = {}
        for r in recent_rejects:
            cat = (r.reason or '').split(' (')[0][:40] or 'unknown'
            key = f'{r.exchange or "?"}/{r.mode or "?"}'
            grouped.setdefault(key, {})
            grouped[key][cat] = grouped[key].get(cat, 0) + 1
        out['rejections_grouped'] = grouped
        out['rejections_total'] = len(recent_rejects)
        # ─── Last 50 WARN/ERROR events for direct inspection ──────────
        last_problem_events = db.scalars(
            select(BotEvent)
            .where(BotEvent.ts >= since)
            .where(BotEvent.level.in_(('WARN', 'ERROR')))
            .order_by(desc(BotEvent.id))
            .limit(50)
        ).all()
        out['recent_events'] = [{
            'ts': e.ts.isoformat() + 'Z',
            'level': e.level,
            'exchange': e.exchange,
            'mode': e.mode,
            'msg': e.message[:400],
        } for e in last_problem_events]
        # ─── Recent trades (last 24h) ─────────────────────────────────
        recent_trades = db.scalars(
            select(Trade).where(Trade.ts >= since).order_by(desc(Trade.id)).limit(50)
        ).all()
        out['recent_trades'] = [{
            'ts': t.ts.isoformat() + 'Z',
            'mode': t.mode,
            'exchange': t.exchange,
            'symbol': t.symbol,
            'venue_leg': t.venue,
            'side': t.side,
            'qty': float(t.quantity or 0),
            'price': float(t.price or 0),
            'fee': float(t.fee or 0),
        } for t in recent_trades]
        out['recent_trades_count'] = len(recent_trades)
        # ─── Rule-based anomaly flags ─────────────────────────────────
        anomalies: list[dict] = []
        # Heartbeat: no cycle event in last hour
        if last_event and (datetime.utcnow() - last_event.ts).total_seconds() > 3600:
            anomalies.append({'severity': 'critical', 'rule': 'no_recent_events', 'detail': f'No bot event in last {(datetime.utcnow() - last_event.ts).total_seconds() / 3600:.1f}h'})
        # Naked spot positions older than 1h
        for n in naked_positions:
            if n['age_minutes'] > 60:
                anomalies.append({'severity': 'warn', 'rule': 'stale_naked_spot', 'detail': f'{n["symbol"]} naked for {n["age_minutes"] / 60:.1f}h (qty={n["quantity"]:.6f}, ~${n["notional_est"]:.2f})'})
        # No trades in the last 24h despite many scans → entry path stuck
        if out['recent_trades_count'] == 0 and out['rejections_total'] > 20:
            anomalies.append({'severity': 'warn', 'rule': 'no_trades_despite_scans', 'detail': f'{out["rejections_total"]} candidates rejected in last {hours}h but ZERO trades executed; entry path likely blocked'})
        # Persistent errors
        if len(recent_errors) > 20:
            anomalies.append({'severity': 'warn', 'rule': 'error_burst', 'detail': f'{len(recent_errors)} ERROR-level events in last {hours}h'})
        # Stuck open position with last_close_error set
        for op in open_positions:
            if op['last_close_error']:
                anomalies.append({'severity': 'warn', 'rule': 'close_blocked', 'detail': f'{op["symbol"]} pos {op["id"]}: {op["last_close_error"][:160]}'})
        out['anomalies'] = anomalies
        out['anomalies_count'] = len(anomalies)
    return out


@app.get('/')
def root():
    return RedirectResponse(url='/dashboard', status_code=303)


def _fmt_ts(dt: datetime | None):
    """Render a UTC datetime as a <time data-utc> element so client-side JS can
    swap it for the browser's local time. The UTC fallback text is what
    JS-disabled clients see; modern browsers will replace .textContent on load.
    """
    if dt is None:
        return Markup('—')
    iso = dt.replace(microsecond=0).isoformat() + 'Z'
    fallback = dt.strftime('%Y-%m-%d %H:%M:%S') + ' UTC'
    return Markup(f'<time data-utc="{iso}">{fallback}</time>')


def _fmt_age(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m'
    if seconds < 86400:
        return f'{seconds // 3600}h{(seconds % 3600) // 60}m'
    return f'{seconds // 86400}d{(seconds % 86400) // 3600}h'


def _current_equity(db, mode: str, gateways=None) -> tuple[float, str, bool]:
    """Returns (equity, source_label_html, is_stale).

    Live mode aggregates across every configured gateway to honour the
    single-pool / multi-venue framing — the snapshot table only carries one
    row per cycle per gateway, so reading just the latest would silently
    drop venues. Paper mode still reads from BalanceSnapshot since there's
    only one virtual book.
    Stale = no fresh data in last 5 minutes."""
    now = datetime.utcnow()
    if mode == MODE_LIVE and gateways:
        total = 0.0
        venues_seen: list[str] = []
        for gw in gateways:
            bals = gw.safe_balances() or {}
            # USDT counts at face value; USDC priced at live USDC/USDT
            # mid (1.0 fallback if the ticker isn't reachable).
            try:
                usdc_rate = gw.safe_price('USDC/USDT') or 1.0
                if not (0.5 < usdc_rate < 2.0):
                    usdc_rate = 1.0
            except Exception:
                usdc_rate = 1.0
            usdt_total = float((bals.get('spot', {}).get('USDT') or {}).get('total') or 0) \
                       + float((bals.get('futures', {}).get('USDT') or {}).get('total') or 0)
            usdc_total = float((bals.get('spot', {}).get('USDC') or {}).get('total') or 0) \
                       + float((bals.get('futures', {}).get('USDC') or {}).get('total') or 0)
            spot_assets = 0.0
            META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
            for asset, bal in (bals.get('spot') or {}).items():
                if asset in META_KEYS or asset in ('USDT', 'USDC') or not isinstance(bal, dict):
                    continue
                qty = float(bal.get('total') or 0)
                if qty <= 0:
                    continue
                px = gw.safe_price(f'{asset}/USDT') or 0
                spot_assets += qty * px
            total += usdt_total + (usdc_total * usdc_rate) + spot_assets
            venues_seen.append(gw.venue_id)
        return total, f'live aggregate across {", ".join(venues_seen)}', False
    snap = db.scalar(select(BalanceSnapshot).where(BalanceSnapshot.source == mode).order_by(desc(BalanceSnapshot.id)).limit(1))
    if snap:
        stale = (now - snap.ts).total_seconds() > 300
        return snap.total_usdt, f'snapshot {snap.source} @ {_fmt_ts(snap.ts)}', stale
    eq = db.scalar(select(EquityCurve).where(EquityCurve.mode == mode).order_by(desc(EquityCurve.id)).limit(1))
    if eq:
        stale = (now - eq.ts).total_seconds() > 300
        return eq.equity_usdt, f'equity_curve @ {_fmt_ts(eq.ts)}', stale
    return 0.0, 'no data yet', True


def _unrealized_for_open(db, gateways, mode: str) -> float:
    """Multi-venue unrealized PnL — pulls each open position's marks from the
    gateway that owns it. Falls back to the first gateway if the position's
    venue isn't currently configured (e.g., credentials revoked)."""
    if not isinstance(gateways, list):
        gateways = [gateways]
    if not gateways:
        return 0.0
    gw_by_venue = {g.venue_id: g for g in gateways}
    open_positions = db.scalars(select(Position).where(Position.status.in_(OPEN_STATUSES), Position.mode == mode)).all()
    total = 0.0
    for p in open_positions:
        pgw = gw_by_venue.get(p.exchange, gateways[0])
        spot_now = pgw.safe_price(p.spot_symbol) or 0.0
        perp_now = pgw.safe_price(p.perp_symbol, perp=True) or 0.0
        if spot_now and perp_now:
            total += position_unrealized_pnl(p, spot_now, perp_now)
    return total


def _shared_ctx(request, view: str, db) -> dict:
    """Context every page needs: view, both mode states (for sidebar tabs),
    worker alive, configured venue list (used by base.html sidebar /
    LIVE banner so neither hard-codes "Binance")."""
    paper_state = get_mode_state(db, MODE_PAPER)
    live_state = get_mode_state(db, MODE_LIVE)
    return {
        'request': request,
        'view': view,
        'paper_state': paper_state,
        'live_state': live_state,
        'worker_alive': _worker_alive(),
        'configured_venues': [g.name for g in make_gateways()],
    }


# ────────── view + per-mode action endpoints ──────────


@app.post('/view/{mode}')
def set_view(mode: str = Path(...), _: None = Depends(auth)):
    if mode not in ALL_MODES:
        raise HTTPException(400, 'invalid mode')
    resp = RedirectResponse(url='/dashboard', status_code=303)
    resp.set_cookie('view', mode, max_age=60 * 60 * 24 * 365, httponly=False)
    return resp


@app.post('/mode/{mode}/stop')
def mode_stop(mode: str = Path(...), _: None = Depends(auth)):
    if mode not in ALL_MODES:
        raise HTTPException(400, 'invalid mode')
    with SessionLocal() as db:
        ms = get_mode_state(db, mode)
        ms.entry_enabled = False
        db.commit()
    return RedirectResponse(url=f'/dashboard?view={mode}', status_code=303)


@app.post('/mode/{mode}/start')
def mode_start(mode: str = Path(...), _: None = Depends(auth)):
    if mode not in ALL_MODES:
        raise HTTPException(400, 'invalid mode')
    with SessionLocal() as db:
        ms = get_mode_state(db, mode)
        ms.entry_enabled = True
        ms.maintenance_mode = False
        db.commit()
    return RedirectResponse(url=f'/dashboard?view={mode}', status_code=303)


@app.post('/mode/{mode}/exit-all-stop')
def mode_exit_all_stop(mode: str = Path(...), _: None = Depends(auth)):
    if mode not in ALL_MODES:
        raise HTTPException(400, 'invalid mode')
    with SessionLocal() as db:
        ms = get_mode_state(db, mode)
        ms.maintenance_mode = True
        ms.entry_enabled = False
        db.commit()
    # Maintenance is processed on the next bot cycle; no synchronous close here so the
    # request returns immediately even if the exchange is slow.
    return RedirectResponse(url=f'/dashboard?view={mode}', status_code=303)


# ─── Per-strategy controls ────────────────────────────────────────────────
# Mirror /mode/{mode}/{stop,start,exit-all-stop} but scoped to a single
# trade-type so the operator can pause Binance same-venue arb without
# touching KuCoin same-venue arb (or, later, cross-venue / onchain).

def _strategy_target(mode: str, trade_type: str):
    if mode not in ALL_MODES:
        raise HTTPException(400, 'invalid mode')
    if trade_type not in TRADE_TYPE_LABELS:
        raise HTTPException(400, 'invalid trade_type')


@app.post('/strategies/{mode}/{trade_type}/stop')
def strategy_stop(mode: str = Path(...), trade_type: str = Path(...), _: None = Depends(auth)):
    """Disable new entries for this strategy. Existing positions stay
    open and continue to be exit-evaluated normally."""
    _strategy_target(mode, trade_type)
    from app.bot import get_strategy_state
    with SessionLocal() as db:
        s = get_strategy_state(db, mode, trade_type)
        s.entry_enabled = False
        db.commit()
    return RedirectResponse(url='/config', status_code=303)


@app.post('/strategies/{mode}/{trade_type}/start')
def strategy_start(mode: str = Path(...), trade_type: str = Path(...), _: None = Depends(auth)):
    """Re-enable new entries for this strategy."""
    _strategy_target(mode, trade_type)
    from app.bot import get_strategy_state
    with SessionLocal() as db:
        s = get_strategy_state(db, mode, trade_type)
        s.entry_enabled = True
        s.exit_all_pending = False
        db.commit()
    return RedirectResponse(url='/config', status_code=303)


@app.post('/strategies/{mode}/{trade_type}/exit-all-stop')
def strategy_exit_all_stop(mode: str = Path(...), trade_type: str = Path(...), _: None = Depends(auth)):
    """Close every open position of this trade-type and disable entries.
    Processed on the next bot cycle so the request returns immediately
    even if a slow venue is in the path."""
    _strategy_target(mode, trade_type)
    from app.bot import get_strategy_state
    with SessionLocal() as db:
        s = get_strategy_state(db, mode, trade_type)
        s.entry_enabled = False
        s.exit_all_pending = True
        db.commit()
    return RedirectResponse(url='/config', status_code=303)


@app.post('/positions/{position_id}/close')
def position_close(position_id: int = Path(...), _: None = Depends(auth)):
    with SessionLocal() as db:
        p = db.get(Position, position_id)
        if p is None or p.status != 'open':
            raise HTTPException(404, 'open position not found')
        cfg = get_strategy_config(db)
        gw = _gateway_for(p.exchange)
        try:
            gw.load_markets()
        except Exception:
            pass
        manual_close(db, gw, p, cfg)
        db.commit()
    return RedirectResponse(url=f'/positions?view={p.mode}', status_code=303)


@app.post('/run-once')
def run_once(view: str | None = Cookie(default=None), _: None = Depends(auth)):
    mode = view if view in ALL_MODES else None  # None = both modes
    run_one_cycle(mode=mode)
    return RedirectResponse(url='/dashboard', status_code=303)


@app.post('/admin/reingest-flows')
def reingest_capital_flows(_: None = Depends(auth)):
    """One-shot: walk every gateway's capital-flow history with a 2-year
    lookback and ingest any rows we don't already have. Used when the
    user expects a known deposit (e.g. master→sub transfer) to show up
    on the dashboard but per-cycle ingest hasn't surfaced it yet.

    Logs a per-endpoint diagnostic to /logs so the user can see exactly
    what each Binance/KuCoin endpoint returned (or refused). Idempotent
    via ``CapitalFlow.external_id`` — safe to call repeatedly."""
    from app.exchange import make_gateways
    from app.bot import _ingest_api_capital_flows, log_event
    gateways = make_gateways()
    with SessionLocal() as db:
        for gw in gateways:
            inserted = _ingest_api_capital_flows(db, gw, MODE_LIVE, lookback_days=730)
            errors = gw.last_history_errors or {}
            if errors:
                err_summary = '; '.join(f'{k}={v[:80]}' for k, v in errors.items())
                log_event(db, f'Re-ingest errors: {err_summary}', mode=MODE_LIVE, level='WARN', exchange=gw.venue_id)
            log_event(db, f'Re-ingest complete · {inserted} new row(s)', mode=MODE_LIVE, exchange=gw.venue_id)
        db.commit()
    return RedirectResponse(url='/dashboard', status_code=303)


# Legacy /mode toggle kept for backward compat — now just sets the view cookie.
@app.post('/mode')
def set_mode(mode: str = Form(...), _: None = Depends(auth)):
    if mode == 'live':
        return set_view(MODE_LIVE)
    return set_view(MODE_PAPER)


# ────────── pages ──────────


@app.get('/dashboard', response_class=HTMLResponse)
def dashboard(request: Request, view: str | None = None, view_cookie: str | None = Cookie(default=None, alias='view'), _: None = Depends(auth)):
    v = _resolve_view(view, view_cookie)
    with SessionLocal() as db:
        ctx = _shared_ctx(request, v, db)
        ctx['active'] = 'dashboard'
        cfg = get_strategy_config(db)
        ctx['cfg'] = cfg
        gateways = make_gateways() or [BinanceGateway()]
        # Default gateway used for symbol price lookups + open-position rendering.
        # Per-position rows below switch to the position's own venue gateway
        # when computing leg states.
        gw = gateways[0]

        # Per-venue equity curve: read up to 60 points per venue from
        # EquityCurve, build a polyline per venue + an aggregated curve
        # (summed at matching cycle indexes). Aggregated is the single-pool
        # truth; per-venue lines let the user see how each venue performed.
        equity_curves_by_venue: dict[str, list] = {}
        for gw in gateways:
            pts = list(reversed(db.scalars(
                select(EquityCurve)
                .where(EquityCurve.mode == v, EquityCurve.exchange == gw.venue_id)
                .order_by(desc(EquityCurve.id))
                .limit(60)
            ).all()))
            if pts:
                equity_curves_by_venue[gw.venue_id] = pts

        equity_curves: list[dict] = []
        agg_xs: list[float] = []
        agg_ys: list[float] = []

        # Stable per-venue colours that match the equity-composition donut.
        VENUE_COLOR = {'binance': '#fbbf24', 'kucoin': '#38bdf8', 'ibkr': '#a78bfa', 'onchain': '#f472b6', 'paper': '#4ade80'}

        if equity_curves_by_venue:
            # Common Y range so all polylines share the same scale.
            all_ys = [pt.equity_usdt for pts in equity_curves_by_venue.values() for pt in pts]
            # Aggregated curve: sum equity across venues at each cycle index
            # (most-recent index = 0 from the right). Length = max len across venues.
            max_len = max(len(pts) for pts in equity_curves_by_venue.values())
            for i in range(max_len):
                total = 0.0
                for pts in equity_curves_by_venue.values():
                    if i < len(pts):
                        total += pts[i].equity_usdt
                agg_xs.append(i)
                agg_ys.append(total)
            all_ys.extend(agg_ys)
            ymin, ymax = min(all_ys), max(all_ys)
            yrange = (ymax - ymin) or 1.0
            xmax = max(1, max_len - 1)
            for venue_id, pts in equity_curves_by_venue.items():
                ys = [pt.equity_usdt for pt in pts]
                xs = list(range(len(pts)))
                polyline = ' '.join(
                    f'{(x / xmax) * 600:.1f},{110 - (y - ymin) / yrange * 100:.1f}'
                    for x, y in zip(xs, ys)
                )
                equity_curves.append({
                    'venue_id': venue_id,
                    'venue_name': next((g.name for g in gateways if g.venue_id == venue_id), venue_id),
                    'color': VENUE_COLOR.get(venue_id, '#888'),
                    'polyline': polyline,
                    'latest': ys[-1],
                    'first': ys[0],
                    'first_ts': _fmt_ts(pts[0].ts),
                    'latest_ts': _fmt_ts(pts[-1].ts),
                })
            agg_polyline = ' '.join(
                f'{(x / xmax) * 600:.1f},{110 - (y - ymin) / yrange * 100:.1f}'
                for x, y in zip(agg_xs, agg_ys)
            )
        else:
            agg_polyline = ''

        # One latest scan per venue — dashboard shows them side-by-side so
        # the user can see at a glance whether each venue's scan is firing.
        latest_scans = []
        for gw in gateways:
            sc = db.scalar(
                select(ScanResult)
                .where(ScanResult.mode == v, ScanResult.exchange == gw.venue_id)
                .order_by(desc(ScanResult.id))
                .limit(1)
            )
            if sc is None:
                continue
            try:
                top = json.loads(sc.top_candidates) or []
            except Exception:
                top = []
            for c in top:
                if 'apr' not in c:
                    c['apr'] = annualize_rate(c.get('fr', 0.0), c.get('interval_h', 8.0))
                c['effective_apy'] = effective_position_apy(c['apr'], cfg.max_perp_leverage or cfg.perp_leverage or 1)
            latest_scans.append({
                'venue_id': gw.venue_id,
                'venue_name': gw.name,
                'venue_color': VENUE_COLOR.get(gw.venue_id, '#888'),
                'ts': _fmt_ts(sc.ts),
                'candidates_total': sc.candidates_total,
                'candidates_passing': sc.candidates_passing,
                'action': sc.action,
                'top': top,
            })
        # Last cycle age uses whichever gateway scanned most recently.
        latest_scan = max(
            (db.scalar(select(ScanResult).where(ScanResult.mode == v, ScanResult.exchange == gw.venue_id).order_by(desc(ScanResult.id)).limit(1)) for gw in gateways),
            key=lambda x: (x.ts if x else datetime.min),
            default=None,
        )

        current_equity, equity_source, equity_stale = _current_equity(db, v, gateways)
        # Aggregate balance-fetch errors across every configured venue so the
        # banner names the venue that's down rather than blanket-saying
        # "Binance" (which would be misleading when KuCoin is the one failing).
        balances_error = None
        if v == MODE_LIVE:
            errors: list[str] = []
            for g in gateways:
                bals = g.safe_balances()
                # If the gateway is in a rate-limit pause we still got a
                # cached value back (possibly stale, possibly None) — note
                # the pause separately so the user knows what's happening.
                if g.is_rate_limited():
                    pause_remaining = max(0, int(g._rate_limit_pause_until - time.time()))
                    errors.append(f'{g.name}: rate-limited, pausing API calls for ~{pause_remaining}s. Showing cached balance.')
                elif g.last_balance_error and bals is not None:
                    # Stale-fallback path: live API failed but we're serving
                    # the last-good cached balance. Tell the user explicitly.
                    errors.append(f'{g.name}: API error ({g.last_balance_error[:120]}). Showing cached balance from last successful fetch.')
                elif bals is None and g.last_balance_error:
                    errors.append(f'{g.name}: {g.last_balance_error[:140]} (no cached balance available)')
            if errors:
                balances_error = ' · '.join(errors)

        trade_realized = total_realized_pnl(db, mode=v)
        funding_income_tracked = total_funding_income(db, mode=v)
        unrealized = _unrealized_for_open(db, gateways, v)
        net_capital, net_capital_meta = net_capital_in(db, mode=v, gateways=gateways)
        flow_count_n = db.scalar(select(func.count(CapitalFlow.id)).where(CapitalFlow.mode == v)) or 0
        xirr_value = portfolio_xirr(db, current_equity, mode=v)
        open_count = db.scalar(select(func.count(Position.id)).where(Position.status.in_(OPEN_STATUSES), Position.mode == v)) or 0
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        trades_today_n = db.scalar(select(func.count(Trade.id)).where(Trade.ts >= today_start, Trade.mode == v)) or 0

        # Total PnL is the **observed** number: how much the live wallet has
        # grown beyond the capital injected. This is the only PnL figure we
        # can guarantee is correct because it doesn't depend on per-row
        # bookkeeping. Trade and funding components are the explanatory
        # breakdown — they sum to total_pnl by construction (see funding
        # inference below).
        total_pnl = current_equity - net_capital

        # In LIVE mode, Binance auto-credits funding payments into the
        # futures wallet — they never appear in the trades table, so
        # total_funding_income() returns 0. Without inferring it, the UI
        # would display "trade -142, funding 0, total -142" while the user's
        # equity is up. Infer funding as the residual:
        #   funding_income = total_pnl - trade_realized - unrealized
        # In paper mode we trust the per-position funding accruals because
        # the bot synthesises them; only live needs the inference.
        if v == MODE_LIVE:
            funding_income = total_pnl - trade_realized - unrealized
        else:
            funding_income = funding_income_tracked
        realized = trade_realized + funding_income

        # Last cycle age = the most recent scan timestamp across all venues.
        all_pts = [pt for pts in equity_curves_by_venue.values() for pt in pts]
        last_cycle_ts = (latest_scan.ts if latest_scan else
                         (max(p.ts for p in all_pts) if all_pts else None))
        last_cycle_age = _fmt_age(datetime.utcnow() - last_cycle_ts) if last_cycle_ts else None

        stuck_positions = db.scalars(select(Position).where(Position.status.in_(OPEN_STATUSES), Position.mode == v, Position.last_close_error != '')).all()
        ctx['stuck_positions'] = [{'symbol': p.symbol, 'err': p.last_close_error[:160]} for p in stuck_positions]

        # Fees paid across every trade in this mode. Avg fee % = total fees
        # / total notional, the realistic round-trip cost the bot is paying.
        # Notional is computed inline (Trade has quantity + price; no stored
        # notional column) so the figures stay consistent if old rows were
        # written before any historical normalisation.
        fee_rows = db.scalars(select(Trade).where(Trade.mode == v)).all()
        total_fees = sum(float(t.fee or 0.0) for t in fee_rows)
        total_notional = sum(float(t.quantity or 0.0) * float(t.price or 0.0) for t in fee_rows)
        avg_fee_pct = (total_fees / total_notional) * 100.0 if total_notional > 0 else 0.0
        fee_breakdown: dict[tuple[str, str], dict] = {}
        for t in fee_rows:
            key = (t.exchange or '?', t.venue or '?')  # ('binance', 'spot') etc.
            slot = fee_breakdown.setdefault(key, {'fees': 0.0, 'notional': 0.0, 'count': 0})
            slot['fees'] += float(t.fee or 0.0)
            slot['notional'] += float(t.quantity or 0.0) * float(t.price or 0.0)
            slot['count'] += 1
        ctx['fees'] = {
            'total_usd': total_fees,
            'avg_pct': avg_fee_pct,
            'count': len(fee_rows),
            'breakdown': sorted(
                ({
                    'exchange': k[0],
                    'leg': k[1],
                    'fees': v_['fees'],
                    'notional': v_['notional'],
                    'pct': (v_['fees'] / v_['notional'] * 100.0) if v_['notional'] > 0 else 0.0,
                    'count': v_['count'],
                } for k, v_ in fee_breakdown.items()),
                key=lambda x: x['fees'], reverse=True,
            ),
        }

        breakdown_items = equity_breakdown(db, gateways, v)
        if v == MODE_PAPER:
            tracked = sum(max(0.0, i['value']) for i in breakdown_items)
            free_cash = max(0.0, current_equity - tracked)
            breakdown_items.insert(0, {'label': 'Paper · Free cash', 'value': free_cash, 'color': '#38bdf8', 'venue': 'paper'})
        # Group by venue so the donut + legend can show "single pool, per-venue
        # breakdown" rather than just a flat list of buckets. Today only
        # Binance + paper are populated; KuCoin / IBKR slot in here later.
        venue_totals: dict[str, float] = {}
        for item in breakdown_items:
            venue_totals[item.get('venue', 'unknown')] = venue_totals.get(item.get('venue', 'unknown'), 0.0) + max(0.0, item['value'])
        ctx['venue_totals'] = sorted(
            ({'venue': vn, 'total': tot} for vn, tot in venue_totals.items() if tot > 0),
            key=lambda x: x['total'], reverse=True,
        )
        ctx['breakdown_items'] = breakdown_items
        ctx['breakdown_donut'] = equity_donut_svg(breakdown_items)
        ctx['equity_stale'] = equity_stale
        # Surface the actual deployable free balances aggregated across venues
        # — this is what the bot can deploy on a fresh entry.
        #
        # Unified-margin caveat: under Binance PM and KuCoin UTA the spot
        # and futures wallets are the SAME unified pool; ``spot.free`` and
        # ``fut.free`` both report ``pool_free``. Summing them would
        # double-count. We detect unified margin via ``fut.total == 0``
        # (set by those gateways' _fetch_balances_uncached) and take
        # spot.free alone in that case. For Classic accounts (still real
        # for IBKR / future venues) we keep the sum.
        if v == MODE_LIVE:
            free_total = 0.0
            free_breakdown: list[dict] = []
            for g in gateways:
                bals_for_display = g.safe_balances() or {}
                # USDC priced at the live USDC/USDT mid (1.0 fallback).
                # The bot never cross-funds USDT ↔ USDC for sizing — the
                # rate is only used here for the headline figure.
                try:
                    rate = g.safe_price('USDC/USDT') or 1.0
                    if not (0.5 < rate < 2.0):
                        rate = 1.0
                except Exception:
                    rate = 1.0
                for q in ('USDT', 'USDC'):
                    spot_free_native = float((bals_for_display.get('spot', {}).get(q) or {}).get('free') or 0)
                    fut_free_native = float((bals_for_display.get('futures', {}).get(q) or {}).get('free') or 0)
                    fut_total_native = float((bals_for_display.get('futures', {}).get(q) or {}).get('total') or 0)
                    # Under unified margin (PM / UTA) fut_total is 0 and
                    # spot_free already accounts for the full pool — adding
                    # fut_free would double-count. Under Classic the two
                    # wallets are separate so their free amounts sum to the
                    # actual deployable.
                    if fut_total_native > 0.001:
                        native_total = spot_free_native + fut_free_native
                    else:
                        native_total = spot_free_native
                    if native_total <= 0.001:
                        continue
                    rate_q = rate if q == 'USDC' else 1.0
                    rate_note = '' if q == 'USDT' else f' × {rate_q:.4f}'
                    free_total += native_total * rate_q
                    free_breakdown.append({
                        'label': f'{g.name} · {q}',
                        'value': native_total * rate_q,
                        'native': native_total,
                        'asset': q,
                        'note': rate_note,
                    })
            ctx['free_deployable_breakdown'] = free_breakdown
        else:
            ctx['free_deployable_breakdown'] = []

        # Open positions rows + leg detail (formerly /positions).
        open_positions = db.scalars(select(Position).where(Position.status.in_(OPEN_STATUSES), Position.mode == v)).all()
        gw_by_venue = {g.venue_id: g for g in gateways}
        rows = []
        for p in open_positions:
            # Pick the gateway that owns this position so prices and leg states
            # come from the right venue's API.
            pgw = gw_by_venue.get(p.exchange, gw)
            spot_now = pgw.safe_price(p.spot_symbol) or 0.0
            perp_now = pgw.safe_price(p.perp_symbol, perp=True) or 0.0
            interval_h = p.funding_interval_hours or 8.0
            leg_states = _position_leg_states(pgw, p) if v == MODE_LIVE else {'spot_alive': True, 'perp_alive': True, 'spot_actual': p.quantity, 'perp_actual': p.quantity}
            entry_trades = db.scalars(select(Trade).where(Trade.position_id == p.id, Trade.side == 'buy', Trade.venue == 'spot')).all() + \
                           db.scalars(select(Trade).where(Trade.position_id == p.id, Trade.side == 'sell', Trade.venue == 'futures')).all()
            spot_entry_trade = next((t for t in entry_trades if t.venue == 'spot'), None)
            perp_entry_trade = next((t for t in entry_trades if t.venue == 'futures'), None)
            spot_leg_pnl = (spot_now - p.spot_entry_price) * p.quantity if spot_now else 0.0
            perp_leg_pnl = (p.perp_entry_price - perp_now) * p.quantity if perp_now else 0.0
            rows.append({
                'id': p.id,
                'symbol': p.symbol,
                'venue': p.exchange,
                'quantity': p.quantity,
                'trade_type': p.trade_type or '',
                'trade_type_label': TRADE_TYPE_LABELS.get(p.trade_type or '', p.trade_type or ''),
                'spot_symbol': p.spot_symbol,
                'perp_symbol': p.perp_symbol,
                'spot_entry': p.spot_entry_price,
                'spot_now': spot_now,
                'perp_entry': p.perp_entry_price,
                'perp_now': perp_now,
                'notional_entry': p.quantity * p.spot_entry_price,
                'notional_now': p.quantity * spot_now if spot_now else 0.0,
                'basis_entry_bps': basis_bps(p.spot_entry_price, p.perp_entry_price),
                'basis_now_bps': basis_bps(spot_now, perp_now) if (spot_now and perp_now) else 0.0,
                'entry_funding_apy': annualize_rate(p.entry_funding_rate, interval_h),
                'last_funding_apy': annualize_rate(p.last_funding_rate, interval_h),
                'effective_apy': effective_position_apy(annualize_rate(p.last_funding_rate, interval_h), cfg.max_perp_leverage or cfg.perp_leverage or 1),
                'interval_hours': interval_h,
                'opened_at': _fmt_ts(p.opened_at),
                'age': _fmt_age(datetime.utcnow() - p.opened_at),
                'unrealized_pnl': position_unrealized_pnl(p, spot_now, perp_now) if (spot_now and perp_now) else 0.0,
                'funding_income': p.funding_income_accrued,
                'last_close_error': p.last_close_error or '',
                'spot_alive': leg_states['spot_alive'],
                'perp_alive': leg_states['perp_alive'],
                'spot_actual': leg_states['spot_actual'],
                'perp_actual': leg_states['perp_actual'],
                'leg_status': ('fully open' if (leg_states['spot_alive'] and leg_states['perp_alive']) else
                               ('spot only' if leg_states['spot_alive'] else
                                ('perp only' if leg_states['perp_alive'] else 'flat (reconcile pending)'))),
                'spot_leg': {
                    'symbol': p.spot_symbol, 'side': 'long', 'qty': p.quantity,
                    'entry_price': p.spot_entry_price, 'now_price': spot_now,
                    'notional_entry': p.quantity * p.spot_entry_price,
                    'notional_now': p.quantity * spot_now if spot_now else 0.0,
                    'fee_paid': float(spot_entry_trade.fee) if spot_entry_trade else 0.0,
                    'mtm_pnl': spot_leg_pnl,
                    'entry_ts': _fmt_ts(spot_entry_trade.ts) if spot_entry_trade else _fmt_ts(p.opened_at),
                },
                'perp_leg': {
                    'symbol': p.perp_symbol, 'side': 'short', 'qty': p.quantity,
                    'entry_price': p.perp_entry_price, 'now_price': perp_now,
                    'notional_entry': p.quantity * p.perp_entry_price,
                    'notional_now': p.quantity * perp_now if perp_now else 0.0,
                    'fee_paid': float(perp_entry_trade.fee) if perp_entry_trade else 0.0,
                    'mtm_pnl': perp_leg_pnl,
                    'funding_income': p.funding_income_accrued,
                    'last_funding_apy': annualize_rate(p.last_funding_rate, interval_h),
                    'interval_hours': interval_h,
                    'entry_ts': _fmt_ts(perp_entry_trade.ts) if perp_entry_trade else _fmt_ts(p.opened_at),
                },
            })

        # Closed positions table (formerly bottom of /positions). For each
        # closed position we surface the full trade detail of all four legs
        # — spot buy + perp sell on entry, spot sell + perp buy on close —
        # so the operator can audit fees, slippage, and timing without
        # cross-referencing the trades table.
        closed_rows = db.scalars(select(Position).where(Position.status == 'closed', Position.mode == v).order_by(desc(Position.id)).limit(20)).all()
        closed = []
        for c in closed_rows:
            trades_for_c = db.scalars(select(Trade).where(Trade.position_id == c.id).order_by(Trade.ts)).all()
            spot_entry = next((t for t in trades_for_c if t.venue == 'spot' and t.side == 'buy'), None)
            spot_exit = next((t for t in trades_for_c if t.venue == 'spot' and t.side == 'sell'), None)
            perp_entry = next((t for t in trades_for_c if t.venue == 'futures' and t.side == 'sell'), None)
            perp_exit = next((t for t in trades_for_c if t.venue == 'futures' and t.side == 'buy'), None)

            def _leg(entry, exit, side: str) -> dict:
                """Pack a leg's entry+exit into a uniform dict the template
                can render. ``side`` is 'long' for spot, 'short' for perp."""
                if entry is None and exit is None:
                    return {'present': False}
                ep = float(entry.price) if entry else 0.0
                xp = float(exit.price) if exit else 0.0
                qty = float((entry or exit).quantity)
                # Long PnL = (exit − entry) × qty; Short PnL = (entry − exit) × qty.
                if side == 'long':
                    leg_pnl = (xp - ep) * qty if (entry and exit) else 0.0
                else:
                    leg_pnl = (ep - xp) * qty if (entry and exit) else 0.0
                return {
                    'present': True,
                    'symbol': (entry or exit).symbol,
                    'side': side,
                    'qty': qty,
                    'entry_price': ep,
                    'exit_price': xp,
                    'entry_notional': ep * qty,
                    'exit_notional': xp * qty,
                    'entry_fee': float(entry.fee) if entry else 0.0,
                    'exit_fee': float(exit.fee) if exit else 0.0,
                    'total_fee': (float(entry.fee) if entry else 0.0) + (float(exit.fee) if exit else 0.0),
                    'leg_pnl': leg_pnl,
                    'entry_ts': _fmt_ts(entry.ts) if entry else None,
                    'exit_ts': _fmt_ts(exit.ts) if exit else None,
                }

            closed.append({
                'id': c.id,
                'symbol': c.symbol,
                'venue': c.exchange,
                'trade_type': c.trade_type or '',
                'trade_type_label': TRADE_TYPE_LABELS.get(c.trade_type or '', c.trade_type or ''),
                'spot_symbol': c.spot_symbol,
                'perp_symbol': c.perp_symbol,
                'quantity': c.quantity,
                'opened_at': _fmt_ts(c.opened_at),
                'closed_at': _fmt_ts(c.closed_at),
                'hold_time': _fmt_age(c.closed_at - c.opened_at) if c.closed_at and c.opened_at else '—',
                'trade_pnl': position_realized_pnl(db, c),
                'funding_income': c.funding_income_accrued,
                'realized': position_realized_pnl(db, c) + c.funding_income_accrued,
                'spot_leg': _leg(spot_entry, spot_exit, 'long'),
                'perp_leg': _leg(perp_entry, perp_exit, 'short'),
                'last_close_error': c.last_close_error or '',
            })

        # Capital flows (formerly on /portfolio).
        flows = db.scalars(select(CapitalFlow).where(CapitalFlow.mode == v).order_by(desc(CapitalFlow.id))).all()

        ctx.update({
            'current_equity': current_equity,
            'equity_source': equity_source,
            'balances_error': balances_error,
            'realized_pnl': realized,
            'trade_pnl': trade_realized,
            'funding_income': funding_income,
            'unrealized_pnl': unrealized,
            'total_pnl': total_pnl,
            'net_capital': net_capital,
            'net_capital_meta': net_capital_meta,
            'flow_count': flow_count_n,
            'xirr_value': xirr_value,
            'open_count': open_count,
            'trades_today': trades_today_n,
            'equity_curves': equity_curves,         # per-venue lines (color-coded)
            'equity_agg_polyline': agg_polyline,    # aggregated single-pool line
            'latest_scans': latest_scans,           # one card per venue
            'last_cycle_age': last_cycle_age,
            'rows': rows,
            'closed': closed,
            'flows': [{
                'id': f.id, 'ts': _fmt_ts(f.ts), 'venue': f.exchange,
                'amount_usdt': f.amount_usdt, 'kind': f.kind,
                'detected_by': f.detected_by, 'note': f.note,
            } for f in flows],
        })
    response = templates.TemplateResponse(request, 'dashboard.html', ctx)
    response.set_cookie('view', v, max_age=60 * 60 * 24 * 365, httponly=False)
    return response


# Legacy routes redirect to the merged /dashboard so existing bookmarks still work.
@app.get('/positions')
def positions_page_redirect(_: None = Depends(auth)):
    return RedirectResponse(url='/dashboard', status_code=303)


@app.get('/portfolio')
def portfolio_page_redirect(_: None = Depends(auth)):
    return RedirectResponse(url='/dashboard', status_code=303)



@app.get('/transactions', response_class=HTMLResponse)
def transactions_page(request: Request, view: str | None = None, limit: int = 100, view_cookie: str | None = Cookie(default=None, alias='view'), _: None = Depends(auth)):
    v = _resolve_view(view, view_cookie)
    with SessionLocal() as db:
        ctx = _shared_ctx(request, v, db)
        ctx['active'] = 'transactions'
        ctx['cfg'] = get_strategy_config(db)

        # Position history — open + closed, newest first. For live positions
        # we route the leg-state probe through the *position's own venue*
        # gateway so KuCoin positions get their leg state from KuCoin and
        # Binance from Binance.
        gateways_by_venue = {g.venue_id: g for g in make_gateways()} if v == MODE_LIVE else {}
        position_rows = db.scalars(select(Position).where(Position.mode == v).order_by(desc(Position.id)).limit(limit)).all()
        positions_v = []
        for p in position_rows:
            trade_pnl = position_realized_pnl(db, p)
            ended = p.closed_at if p.closed_at else datetime.utcnow()
            hold = ended - p.opened_at
            # Per-leg state from the position's venue (live only). Closed
            # positions show both legs as flat since there's nothing to verify.
            if v == MODE_LIVE and p.status == 'open' and p.exchange in gateways_by_venue:
                st = _position_leg_states(gateways_by_venue[p.exchange], p)
                spot_alive = st['spot_alive']
                perp_alive = st['perp_alive']
            elif p.status == 'open':
                spot_alive = perp_alive = True  # paper assumes both are good
            else:
                spot_alive = perp_alive = False  # closed → both flat
            if p.status == 'closed':
                leg_label = 'closed'
            elif spot_alive and perp_alive:
                leg_label = 'fully open'
            elif spot_alive:
                leg_label = 'spot only (naked long)'
            elif perp_alive:
                leg_label = 'perp only (naked short)'
            else:
                leg_label = 'flat (reconcile pending)'
            positions_v.append({
                'id': p.id,
                'symbol': p.symbol,
                'venue': p.exchange,
                'status': p.status,
                'leg_label': leg_label,
                'spot_alive': spot_alive,
                'perp_alive': perp_alive,
                'quantity': p.quantity,
                'notional_entry': p.quantity * p.spot_entry_price,
                'spot_entry': p.spot_entry_price,
                'perp_entry': p.perp_entry_price,
                'opened_at': _fmt_ts(p.opened_at),
                'closed_at': _fmt_ts(p.closed_at) if p.closed_at else None,
                'hold_time': _fmt_age(hold),
                'trade_pnl': trade_pnl,
                'funding_income': p.funding_income_accrued,
                'realized': trade_pnl + p.funding_income_accrued,
                'last_close_error': p.last_close_error or '',
            })

        # Trades — last `limit`, filtered by mode.
        trade_rows = db.scalars(select(Trade).where(Trade.mode == v).order_by(desc(Trade.id)).limit(limit)).all()
        trades_v = [{
            'id': t.id, 'ts': _fmt_ts(t.ts), 'symbol': t.symbol,
            'exchange': t.exchange, 'leg': t.venue,
            'side': t.side, 'quantity': t.quantity, 'price': t.price,
            'notional': t.quantity * t.price, 'fee': t.fee, 'position_id': t.position_id,
        } for t in trade_rows]

        # Transfers / redeems / sweeps — parsed from BotEvent messages. No
        # dedicated table; we substring-match the action verbs the bot logs.
        TRANSFER_KEYWORDS = ('Auto-transferred', 'Pre-close: transferred', 'Pre-close: redeemed', 'Rebalance:', 'Swept ', 'Redeemed ', 'Funded close margin', 'futures→spot', 'spot→futures')
        seen: set[int] = set()
        transfers_v: list[dict] = []
        for keyword in TRANSFER_KEYWORDS:
            for e in db.scalars(select(BotEvent).where(BotEvent.mode == v, BotEvent.message.like(f'%{keyword}%')).order_by(desc(BotEvent.id)).limit(limit)).all():
                if e.id in seen:
                    continue
                seen.add(e.id)
                transfers_v.append({'id': e.id, 'ts_raw': e.ts, 'ts': _fmt_ts(e.ts), 'level': e.level, 'message': e.message})
        transfers_v.sort(key=lambda x: x['ts_raw'], reverse=True)
        transfers_v = transfers_v[:limit]

        # Capital flows.
        flow_rows = db.scalars(select(CapitalFlow).where(CapitalFlow.mode == v).order_by(desc(CapitalFlow.id)).limit(limit)).all()
        flows_v = [{
            'id': f.id, 'ts': _fmt_ts(f.ts), 'venue': f.exchange,
            'amount_usdt': f.amount_usdt, 'kind': f.kind,
            'detected_by': f.detected_by, 'note': f.note,
        } for f in flow_rows]

        ctx.update({'positions': positions_v, 'trades': trades_v, 'transfers': transfers_v, 'flows': flows_v, 'limit': limit})
    response = templates.TemplateResponse(request, 'transactions.html', ctx)
    response.set_cookie('view', v, max_age=60 * 60 * 24 * 365, httponly=False)
    return response


@app.get('/logs', response_class=HTMLResponse)
def logs_page(
    request: Request,
    view: str | None = None,
    view_cookie: str | None = Cookie(default=None, alias='view'),
    reject_q: str | None = None,
    _: None = Depends(auth),
):
    v = _resolve_view(view, view_cookie)
    with SessionLocal() as db:
        ctx = _shared_ctx(request, v, db)
        ctx['active'] = 'logs'
        ctx['cfg'] = get_strategy_config(db)
        scans_raw = db.scalars(select(ScanResult).where(ScanResult.mode == v).order_by(desc(ScanResult.id)).limit(50)).all()
        scans = []
        for s in scans_raw:
            top_label = ''
            try:
                top = json.loads(s.top_candidates) or []
                if top:
                    apy = top[0].get('apr')  # legacy field name; the value is now compounded APY
                    if apy is None:
                        apy = annualize_rate(top[0].get('fr', 0.0), top[0].get('interval_h', 8.0))
                    eff = effective_position_apy(apy, ctx['cfg'].max_perp_leverage or ctx['cfg'].perp_leverage or 1)
                    top_label = f"{top[0]['perp']} @ {apy*100:.2f}% funding APY ({eff*100:.2f}% effective)"
            except Exception:
                pass
            scans.append({
                'ts': _fmt_ts(s.ts), 'venue': s.exchange,
                'candidates_total': s.candidates_total, 'candidates_passing': s.candidates_passing,
                'action': s.action, 'top_candidate_label': top_label, 'note': s.note,
            })

        events = db.scalars(select(BotEvent).where(BotEvent.mode == v).order_by(desc(BotEvent.id)).limit(100)).all()
        events_v = [{'ts': _fmt_ts(e.ts), 'venue': e.exchange, 'level': e.level, 'message': e.message} for e in events]

        # Rejected candidates: server-side search over the WHOLE
        # retention window when ``reject_q`` is set (so the operator
        # can debug why a given symbol never opened, e.g. typing
        # "TRIA" returns every reason TRIA was rejected over the
        # last N days). The cycle loop prunes rows older than
        # REJECTED_RETENTION_DAYS so this stays tractable.
        REJECTED_RETENTION_DAYS = 7
        REJECTED_SEARCH_CAP = 5000
        rq = (reject_q or '').strip()
        rejected_total = db.scalar(select(func.count(RejectedCandidate.id)).where(RejectedCandidate.mode == v)) or 0
        rejected_stmt = select(RejectedCandidate).where(RejectedCandidate.mode == v)
        if rq:
            like = f'%{rq}%'
            rejected_stmt = rejected_stmt.where(
                RejectedCandidate.symbol.ilike(like) | RejectedCandidate.reason.ilike(like)
            )
            rejected_stmt = rejected_stmt.order_by(desc(RejectedCandidate.id)).limit(REJECTED_SEARCH_CAP)
        else:
            rejected_stmt = rejected_stmt.order_by(desc(RejectedCandidate.id)).limit(50)
        rejected = db.scalars(rejected_stmt).all()
        rejected_v = [{'ts': _fmt_ts(r.ts), 'venue': r.exchange, 'symbol': r.symbol, 'reason': r.reason, 'funding_rate': r.funding_rate} for r in rejected]
        ctx['reject_q'] = rq
        ctx['rejected_total'] = rejected_total
        ctx['retention_days'] = REJECTED_RETENTION_DAYS
        ctx['search_cap'] = REJECTED_SEARCH_CAP

        trades = db.scalars(select(Trade).where(Trade.mode == v).order_by(desc(Trade.id)).limit(30)).all()
        trades_v = [{'ts': _fmt_ts(t.ts), 'symbol': t.symbol, 'exchange': t.exchange, 'leg': t.venue, 'side': t.side, 'quantity': t.quantity, 'price': t.price, 'fee': t.fee} for t in trades]

        ctx.update({'scans': scans, 'events': events_v, 'rejected': rejected_v, 'trades': trades_v})
    response = templates.TemplateResponse(request, 'logs.html', ctx)
    response.set_cookie('view', v, max_age=60 * 60 * 24 * 365, httponly=False)
    return response


@app.get('/config', response_class=HTMLResponse)
def config_page(request: Request, saved: int = 0, view: str | None = None, strategy: str | None = None, view_cookie: str | None = Cookie(default=None, alias='view'), _: None = Depends(auth)):
    v = _resolve_view(view, view_cookie)
    from app.bot import get_strategy_state
    # Active strategies the operator can edit config for. The form's
    # per-strategy fields show values from THIS strategy's row; the
    # global fields are shared across all strategies. See SYSTEM.md §4.
    ACTIVE_STRATEGY_TYPES = ('binance_same_venue_funding_arb', 'kucoin_same_venue_funding_arb')
    selected_strategy = strategy if strategy in ACTIVE_STRATEGY_TYPES else ACTIVE_STRATEGY_TYPES[0]
    with SessionLocal() as db:
        ctx = _shared_ctx(request, v, db)
        ctx['active'] = 'config'
        # cfg is the MergedConfig view for `selected_strategy`. The form's
        # per-strategy inputs show that strategy's values; the global
        # inputs come from the underlying global StrategyConfig.
        ctx['cfg'] = get_strategy_config(db, trade_type=selected_strategy)
        ctx['selected_strategy'] = selected_strategy
        ctx['active_strategy_types'] = ACTIVE_STRATEGY_TYPES
        ctx['saved'] = bool(saved)
        # Per-strategy controls. We surface every trade type from the
        # taxonomy — active ones (Binance / KuCoin same-venue) get real
        # buttons; placeholder ones (cross-venue, onchain, IBKR) show as
        # "not yet wired" so the operator can see the roadmap. The
        # ``mode`` axis distinguishes paper vs live so the operator can
        # run paper experiments while live is locked down.
        strategies = []
        active_types = ('binance_same_venue_funding_arb', 'kucoin_same_venue_funding_arb')
        for tt, label in TRADE_TYPE_LABELS.items():
            row = {
                'trade_type': tt,
                'label': label,
                'is_active_strategy': tt in active_types,
                'paper': None,
                'live': None,
            }
            if tt in active_types:
                ps = get_strategy_state(db, 'paper', tt)
                ls = get_strategy_state(db, 'live', tt)
                # Open-position counts let the UI show "X open" next to
                # exit-all-stop so the operator knows what's about to
                # close.
                paper_open = db.scalar(select(func.count(Position.id)).where(
                    Position.status.in_(OPEN_STATUSES), Position.mode == 'paper', Position.trade_type == tt,
                )) or 0
                live_open = db.scalar(select(func.count(Position.id)).where(
                    Position.status.in_(OPEN_STATUSES), Position.mode == 'live', Position.trade_type == tt,
                )) or 0
                row['paper'] = {'entry_enabled': ps.entry_enabled, 'exit_all_pending': ps.exit_all_pending, 'open_count': paper_open}
                row['live'] = {'entry_enabled': ls.entry_enabled, 'exit_all_pending': ls.exit_all_pending, 'open_count': live_open}
            strategies.append(row)
        ctx['strategies'] = strategies
    return templates.TemplateResponse(request, 'config.html', ctx)


@app.post('/config')
def save_config(
    # Which strategy's per-strategy fields this POST updates. Global
    # fields are written to the singleton StrategyConfig regardless;
    # per-strategy fields go to the StrategyConfigPerStrategy row keyed
    # by this trade_type. See SYSTEM.md §4.
    strategy: str = Form('binance_same_venue_funding_arb'),
    # ``*_pct`` form fields are user-typed in PERCENT units (e.g. 20 for
    # 20%) and we convert to decimal here. Underlying schema fields stay
    # in decimal so legacy callers + comparisons elsewhere don't break.
    # NEW form param names after the entry/exit threshold rename.
    # Aliased so old POSTs from cached browser tabs still work.
    entry_min_net_apy_pct: float = Form(None),
    exit_min_net_apy_pct: float = Form(None),
    entry_funding_threshold_pct: float = Form(None),
    exit_funding_threshold_pct: float = Form(None),
    stop_loss_pct_pct: float = Form(...),
    min_position_pct_pct: float = Form(...),
    max_position_pct_pct: float = Form(...),
    futures_buffer_pct_pct: float = Form(...),
    # Plain integer / decimal fields below.
    max_open_positions: int = Form(...),
    max_trades_per_day: int = Form(...),
    max_hold_hours: int = Form(...),
    loop_seconds: int = Form(...),
    paper_slippage_bps: float = Form(...),
    paper_fee_bps: float = Form(...),
    paper_starting_equity: float = Form(...),
    max_exit_basis_bps: float = Form(...),
    exit_basis_buffer_multiple: float = Form(3.0),
    enforce_hedge_check: int = Form(...),
    delisting_check: int = Form(...),
    auto_transfer_enabled: int = Form(...),
    max_perp_leverage: int = Form(1),
    _: None = Depends(auth),
):
    ACTIVE_STRATEGY_TYPES = ('binance_same_venue_funding_arb', 'kucoin_same_venue_funding_arb')
    target_strategy = strategy if strategy in ACTIVE_STRATEGY_TYPES else ACTIVE_STRATEGY_TYPES[0]
    with SessionLocal() as db:
        # cfg is the MergedConfig view for the strategy being edited. Writes
        # to per-strategy fields land on the StrategyConfigPerStrategy row;
        # writes to global fields land on the singleton StrategyConfig.
        cfg = get_strategy_config(db, trade_type=target_strategy)
        # Percent → decimal conversions.
        # Accept either the new (entry_min_net_apy_pct) or legacy
        # (entry_funding_threshold_pct) form name — whichever is non-None.
        entry_apy_pct = entry_min_net_apy_pct if entry_min_net_apy_pct is not None else entry_funding_threshold_pct
        exit_apy_pct = exit_min_net_apy_pct if exit_min_net_apy_pct is not None else exit_funding_threshold_pct
        if entry_apy_pct is None or exit_apy_pct is None:
            return RedirectResponse(url='/config?error=missing_threshold', status_code=303)
        cfg.entry_min_net_apy = entry_apy_pct / 100.0
        cfg.exit_min_net_apy = exit_apy_pct / 100.0
        cfg.stop_loss_pct = stop_loss_pct_pct / 100.0
        cfg.min_position_pct = min_position_pct_pct / 100.0
        cfg.max_position_pct = max_position_pct_pct / 100.0
        cfg.futures_buffer_pct = max(0.05, min(1.0, futures_buffer_pct_pct / 100.0))
        # Plain values.
        cfg.max_open_positions = max_open_positions
        cfg.max_trades_per_day = max_trades_per_day
        cfg.max_hold_hours = max_hold_hours
        cfg.loop_seconds = max(5, loop_seconds)
        cfg.paper_slippage_bps = paper_slippage_bps
        cfg.paper_fee_bps = paper_fee_bps
        cfg.paper_starting_equity = paper_starting_equity
        cfg.max_exit_basis_bps = max_exit_basis_bps
        cfg.exit_basis_buffer_multiple = max(0.0, exit_basis_buffer_multiple)
        cfg.enforce_hedge_check = bool(enforce_hedge_check)
        cfg.delisting_check = bool(delisting_check)
        cfg.auto_transfer_enabled = bool(auto_transfer_enabled)
        new_lev = max(1, max_perp_leverage or 1)
        cfg.max_perp_leverage = new_lev
        cfg.perp_leverage = new_lev  # legacy mirror
        db.commit()
    return RedirectResponse(url=f'/config?saved=1&strategy={target_strategy}', status_code=303)


@app.get('/monitoring', response_class=HTMLResponse)
def monitoring_page(request: Request, view: str | None = None, view_cookie: str | None = Cookie(default=None, alias='view'), _: None = Depends(auth)):
    """Operational monitoring — raw API state for every configured exchange.

    Each card runs a probe against a specific endpoint and shows whether it
    succeeded plus the raw response (truncated). Useful for diagnosing
    permission / IP / connectivity issues without diving into the bot logs.
    Renders synchronously so the request takes a few seconds; that's fine
    here because it's a debug page, not a hot path."""
    v = _resolve_view(view, view_cookie)
    with SessionLocal() as db:
        ctx = _shared_ctx(request, v, db)
        ctx['active'] = 'monitoring'
        ctx['cfg'] = get_strategy_config(db)
        ctx['exchanges'] = _gather_exchange_status()
    return templates.TemplateResponse(request, 'monitoring.html', ctx)


def _mask(secret: str, keep: int = 4) -> str:
    if not secret:
        return '<not set>'
    if len(secret) <= keep * 2:
        return '*' * len(secret)
    return f'{secret[:keep]}…{secret[-keep:]} ({len(secret)} chars)'


def _truncate_json(value, max_chars: int = 4000) -> str:
    """Pretty-print `value` as JSON, truncated to `max_chars` for the UI."""
    try:
        text = json.dumps(value, indent=2, default=str, sort_keys=True)
    except Exception as e:
        text = f'<unable to serialize: {e}>'
    if len(text) > max_chars:
        return text[:max_chars] + f'\n… ({len(text) - max_chars} more chars truncated)'
    return text


def _json_safe(v):
    """Recursively coerce datetimes (and other non-JSON types) into
    plain JSON-serializable values. Used by ``_probe`` so the
    monitoring template's ``tojson`` filter doesn't 500 when a probe
    returns rows that include ``datetime`` objects (e.g. capital-flow
    ingest)."""
    if isinstance(v, datetime):
        return v.isoformat() + 'Z'
    if isinstance(v, dict):
        return {k: _json_safe(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_json_safe(x) for x in v]
    return v


def _probe(label: str, fn) -> dict:
    """Run a probe callable and capture (ok, raw, err, latency_ms).
    The ``raw`` payload is sanitised through :func:`_json_safe` so the
    monitoring template can render it via Jinja's ``tojson`` filter
    without choking on datetime / set / Decimal values.

    For probes that return collections (lists, dicts with ``rows`` /
    ``data``), we annotate the label with the row count so the operator
    can see at a glance whether the endpoint had anything — the most
    common diagnostic question (e.g. "did the deposit-history call
    return rows?") is now answered without expanding the probe."""
    started = time.time()
    try:
        raw = fn()
        annotated = label
        # Try to surface the row count cheaply.
        rows_for_count = raw
        if isinstance(raw, dict):
            rows_for_count = raw.get('rows') or raw.get('data') or raw.get('items')
        if isinstance(rows_for_count, list):
            annotated = f'{label} · {len(rows_for_count)} row(s)'
        return {'label': annotated, 'ok': True, 'raw': _json_safe(raw), 'err': '', 'latency_ms': int((time.time() - started) * 1000)}
    except Exception as e:
        return {'label': label, 'ok': False, 'raw': None, 'err': str(e)[:400], 'latency_ms': int((time.time() - started) * 1000)}


def _gather_exchange_status() -> list[dict]:
    """Build the per-venue section list rendered on /monitoring. The system
    treats the whole portfolio as a single pool of capital distributed across
    venues — this function is the seam where each venue's gateway exposes its
    slice of the pool (capital subtotal + raw API probes). Adding KuCoin /
    Interactive Brokers means appending another section here once their
    gateways land. The structure is intentionally generic."""
    sections: list[dict] = []

    # ---- Binance ----
    gw = BinanceGateway()
    has_creds = bool(settings.binance_api_key and settings.binance_api_secret)
    probes: list[dict] = []
    capital_subtotal = 0.0
    capital_breakdown: list[dict] = []
    # Skip live API calls when every strategy that touches this venue is
    # disabled — saves rate-limit budget for venues that ARE active. The
    # operator sees a clear "venue inactive" status instead of probe
    # results.
    from app.bot import venue_is_active as _venue_is_active
    bn_active = True
    if has_creds:
        with SessionLocal() as _db_check:
            bn_active = _venue_is_active(_db_check, 'binance')
    if has_creds and bn_active:
        probes.append(_probe('Spot fetch_balance()', lambda: gw.spot.fetch_balance()))
        probes.append(_probe('Futures fetch_balance()', lambda: gw.futures.fetch_balance()))
        probes.append(_probe('Deposit history (USDT, 30d)', lambda: gw.deposit_history('USDT', lookback_days=30, ttl_seconds=0)))
        probes.append(_probe('Withdrawal history (USDT, 30d)', lambda: gw.withdrawal_history('USDT', lookback_days=30, ttl_seconds=0)))
        probes.append(_probe('Sub-account transfer in (USDT, 30d)', lambda: gw.sub_account_transfer_history('USDT', incoming=True, lookback_days=30, ttl_seconds=0)))
        probes.append(_probe('Sub-account transfer out (USDT, 30d)', lambda: gw.sub_account_transfer_history('USDT', incoming=False, lookback_days=30, ttl_seconds=0)))
        # Alternate sub-transfer endpoint name. Some Binance vintages
        # expose subUserHistory but not subTransferHistory; probe both
        # so we don't miss rows behind a naming difference.
        probes.append(_probe('Sub-account subUserHistory (alt endpoint)', lambda: gw._call_sapi((
            'sapiV1GetSubAccountTransferSubUserHistory',
            'sapi_v1_get_sub_account_transfer_sub_user_history',
            'sapiGetSubAccountTransferSubUserHistory',
        ), {'asset': 'USDT', 'startTime': int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)})))
        # Universal-transfer history (raw, no intra-account filter) so the
        # operator can see EVERY row Binance returns and spot the user's
        # deposit even if our parser missed it.
        probes.append(_probe('Universal transfers raw (USDT, 30d, paged)', lambda: gw.spot.fetch_transfers('USDT', since=int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000)) or []))
        # PM account snapshot — confirms we're talking to a PM-enabled key
        # and surfaces the totalWalletBalance / totalEquity for cross-check.
        probes.append(_probe('PM /papi/v1/account', lambda: gw._papi(gw.spot, ('papiGetAccount', 'papi_get_account'))({}) if gw._papi(gw.spot, ('papiGetAccount', 'papi_get_account')) else 'papiGetAccount not in this ccxt build'))
        probes.append(_probe('Open perp positions', lambda: gw.open_perp_positions_raw()))
        probes.append(_probe('Capital-flow ingest (deposits + withdrawals + sub-transfers, 365d)', lambda: {
            'rows': gw.list_capital_flow_records(lookback_days=30),
            'errors': gw.last_history_errors,
        }))
        # Capital subtotal: cash + spot assets + futures (all USDT-denominated).
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
        capital_breakdown = [
            {'label': 'Spot · USDT', 'value': spot_usdt},
            {'label': 'Spot · assets', 'value': spot_assets},
            {'label': 'Futures · USDT', 'value': fut_usdt},
        ]
        capital_subtotal = spot_usdt + spot_assets + fut_usdt
    bn_account_label = bn_account_detail = ''
    if has_creds and bn_active:
        try:
            bn_account_label, bn_account_detail = gw.account_type()
        except Exception as e:
            bn_account_label, bn_account_detail = 'Unknown', f'probe error: {str(e)[:80]}'
    sections.append({
        'name': 'Binance',
        'venue_id': 'binance',
        'configured': has_creds,
        'key_masked': _mask(settings.binance_api_key),
        'secret_masked': _mask(settings.binance_api_secret),
        'extra_creds': ([
            {'label': 'Account type (live)', 'value': f'{bn_account_label} — {bn_account_detail}' if bn_account_label else '<not probed>'},
            {'label': 'Rate-limit pause', 'value': (f'PAUSED — {int(gw._rate_limit_pause_until - time.time())}s remaining (consecutive 429s: {gw._rate_limit_consecutive})' if gw.is_rate_limited() else 'none')},
        ] if has_creds else []),
        'probes': probes,
        'last_balance_error': gw.last_balance_error,
        'capital_subtotal_usdt': capital_subtotal,
        'capital_breakdown': capital_breakdown,
        'is_active': bn_active,
        'role': ('spot + USDM-perp arb (active)' if bn_active else 'spot + USDM-perp arb (INACTIVE — all strategies disabled)'),
    })

    # ---- KuCoin ----
    kc_key = getattr(settings, 'kucoin_api_key', '')
    kc_secret = getattr(settings, 'kucoin_api_secret', '')
    kc_pass = getattr(settings, 'kucoin_api_passphrase', '')
    kc_configured = bool(kc_key and kc_secret and kc_pass)
    kc_probes: list[dict] = []
    kc_capital_subtotal = 0.0
    kc_capital_breakdown: list[dict] = []
    kc_balance_err = ''
    kc_active = True
    if kc_configured:
        with SessionLocal() as _db_check:
            kc_active = _venue_is_active(_db_check, 'kucoin')
        kgw = KuCoinGateway()
    if kc_configured and kc_active:
        kc_probes.append(_probe('Spot fetch_balance(trade)', lambda: kgw.spot.fetch_balance({'type': 'trade'})))
        kc_probes.append(_probe('Spot fetch_balance(main)', lambda: kgw.spot.fetch_balance({'type': 'main'})))
        kc_probes.append(_probe('Futures fetch_balance()', lambda: kgw.futures.fetch_balance()))
        kc_probes.append(_probe('Funding rates (markets-derived)', lambda: kgw.funding_rates_dict()))
        kc_probes.append(_probe('Open perp positions', lambda: kgw.open_perp_positions_raw()))
        kc_probes.append(_probe('Deposit history (USDT, 30d)', lambda: kgw.spot.fetch_deposits('USDT', since=int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000))))
        kc_probes.append(_probe('Withdrawal history (USDT, 30d)', lambda: kgw.spot.fetch_withdrawals('USDT', since=int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000))))
        kc_probes.append(_probe('Universal transfers raw (USDT, 30d)', lambda: kgw.spot.fetch_transfers('USDT', since=int((datetime.utcnow() - timedelta(days=30)).timestamp() * 1000))))
        # UTA account-mode probe — surfaces the live account state so the
        # operator can confirm the API key sees UTA/Classic correctly.
        kc_probes.append(_probe('Account mode (UTA check)', lambda: {'is_uta_enabled': kgw.spot.is_uta_enabled()}))
        kc_probes.append(_probe('Capital-flow ingest (deposits + sub-transfers)', lambda: {
            'rows': kgw.list_capital_flow_records(lookback_days=30),
            'errors': kgw.last_history_errors,
        }))
        kc_bals = kgw.safe_balances() or {}
        kc_balance_err = kgw.last_balance_error
        kc_spot_usdt = float((kc_bals.get('spot', {}).get('USDT') or {}).get('total') or 0)
        kc_fut_usdt = float((kc_bals.get('futures', {}).get('USDT') or {}).get('total') or 0)
        kc_spot_assets = 0.0
        META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
        for asset, bal in (kc_bals.get('spot') or {}).items():
            if asset in META_KEYS or asset == 'USDT' or not isinstance(bal, dict):
                continue
            qty = float(bal.get('total') or 0)
            if qty <= 0:
                continue
            px = kgw.safe_price(f'{asset}/USDT') or 0
            kc_spot_assets += qty * px
        kc_capital_breakdown = [
            {'label': 'Spot · USDT (trade wallet)', 'value': kc_spot_usdt},
            {'label': 'Spot · assets', 'value': kc_spot_assets},
            {'label': 'Futures · USDT (contract wallet)', 'value': kc_fut_usdt},
        ]
        kc_capital_subtotal = kc_spot_usdt + kc_spot_assets + kc_fut_usdt
    kc_account_label = kc_account_detail = ''
    if kc_configured and kc_active:
        try:
            kc_account_label, kc_account_detail = kgw.account_type()
        except Exception as e:
            kc_account_label, kc_account_detail = 'Unknown', f'probe error: {str(e)[:80]}'
    kc_extra = [{'label': 'Passphrase', 'value': _mask(kc_pass)}]
    if kc_configured:
        kc_extra.append({'label': 'Account type (live)', 'value': f'{kc_account_label} — {kc_account_detail}'})
        kc_extra.append({'label': 'Rate-limit pause', 'value': (f'PAUSED — {int(kgw._rate_limit_pause_until - time.time())}s remaining (consecutive 429s: {kgw._rate_limit_consecutive})' if kgw.is_rate_limited() else 'none')})
    sections.append({
        'name': 'KuCoin',
        'venue_id': 'kucoin',
        'configured': kc_configured,
        'key_masked': _mask(kc_key),
        'secret_masked': _mask(kc_secret),
        'extra_creds': kc_extra,
        'probes': kc_probes,
        'last_balance_error': kc_balance_err,
        'capital_subtotal_usdt': kc_capital_subtotal,
        'capital_breakdown': kc_capital_breakdown,
        'is_active': kc_active,
        'role': ('spot + USDT-perp arb' + (
            ' (active)' if kc_configured and kc_active else
            (' (INACTIVE — all strategies disabled)' if kc_configured else ' (set credentials to activate)')
        )),
    })

    # ---- Interactive Brokers (future — for cross-asset arb / equities) ----
    sections.append({
        'name': 'Interactive Brokers',
        'venue_id': 'ibkr',
        'configured': False,
        'key_masked': '<not yet wired>',
        'secret_masked': '<not yet wired>',
        'extra_creds': [],
        'probes': [],
        'last_balance_error': '',
        'capital_subtotal_usdt': 0.0,
        'capital_breakdown': [],
        'role': 'cross-asset / equities (future) — for high-funding stock perps like INCL, MSTR',
    })

    # ---- Onchain (future — DEX perps) ---------------------------------
    # Hyperliquid, Drift, Aevo, GMX-class venues. Wallet keys + RPC endpoint
    # go here once we pick a primary protocol. Placeholder section so the
    # maintenance UI shows the venue is on the roadmap.
    sections.append({
        'name': 'Onchain',
        'venue_id': 'onchain',
        'configured': False,
        'key_masked': '<not yet wired>',
        'secret_masked': '<not yet wired>',
        'extra_creds': [{'label': 'Wallet address', 'value': '<not yet set>'}, {'label': 'RPC endpoint', 'value': '<not yet set>'}],
        'probes': [],
        'last_balance_error': '',
        'capital_subtotal_usdt': 0.0,
        'capital_breakdown': [],
        'role': 'DEX perps + yield-bearing collateral (future) — Hyperliquid / Drift / Aevo class',
    })

    return sections


@app.get('/monitoring/export.md', response_class=PlainTextResponse)
def monitoring_export(view: str | None = None, view_cookie: str | None = Cookie(default=None, alias='view'), _: None = Depends(auth)):
    """Single-shot markdown dump of the bot's full state for diagnostic
    sharing. Includes: configuration, mode states, open + closed
    positions (with leg states), recent trades / events / capital flows,
    equity composition, current Binance probe results, and the outbound IP.

    Renders synchronously, hitting Binance for the live probes — slow on
    first call but acceptable for a manual download."""
    v = _resolve_view(view, view_cookie)
    body = _render_export_md(v)
    return PlainTextResponse(
        body,
        media_type='text/markdown; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="autotrader_export_{v}_{datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")}.md"'},
    )


def _md_table(headers: list[str], rows: list[list]) -> str:
    if not rows:
        return '_no rows_\n'
    lines = ['| ' + ' | '.join(headers) + ' |',
             '|' + '|'.join(['---'] * len(headers)) + '|']
    for r in rows:
        lines.append('| ' + ' | '.join(_md_cell(c) for c in r) + ' |')
    return '\n'.join(lines) + '\n'


def _md_cell(value) -> str:
    if value is None:
        return ''
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M:%S') + 'Z'
    if isinstance(value, float):
        return f'{value:.6g}'
    text = str(value).replace('|', '\\|').replace('\n', ' ')
    return text[:240]


def _render_export_md(v: str) -> str:
    now = datetime.utcnow()
    parts: list[str] = []
    parts.append(f'# AutoTrader export — view=`{v}` — generated {now.strftime("%Y-%m-%d %H:%M:%S")}Z\n')

    with SessionLocal() as db:
        cfg = get_strategy_config(db)
        paper_state = get_mode_state(db, MODE_PAPER)
        live_state = get_mode_state(db, MODE_LIVE)

        # ---- Configuration ----
        parts.append('## Strategy configuration\n')
        cfg_rows = [
            ['entry_min_net_apy', cfg.entry_min_net_apy],
            ['exit_min_net_apy', cfg.exit_min_net_apy],
            ['stop_loss_pct', cfg.stop_loss_pct],
            ['max_open_positions', cfg.max_open_positions],
            ['max_trades_per_day', cfg.max_trades_per_day],
            ['min_position_pct', cfg.min_position_pct],
            ['max_position_pct', cfg.max_position_pct],
            ['max_hold_hours', cfg.max_hold_hours],
            ['loop_seconds', cfg.loop_seconds],
            ['paper_slippage_bps', cfg.paper_slippage_bps],
            ['paper_fee_bps', cfg.paper_fee_bps],
            ['paper_starting_equity', cfg.paper_starting_equity],
            ['max_exit_basis_bps', cfg.max_exit_basis_bps],
            ['enforce_hedge_check', cfg.enforce_hedge_check],
            ['delisting_check', cfg.delisting_check],
            ['auto_transfer_enabled', cfg.auto_transfer_enabled],
            ['perp_leverage', cfg.perp_leverage],
        ]
        parts.append(_md_table(['key', 'value'], cfg_rows))

        # ---- Mode states ----
        parts.append('\n## Mode states\n')
        parts.append(_md_table(
            ['mode', 'entry_enabled', 'exit_enabled', 'maintenance_mode', 'updated_at'],
            [[m.mode, m.entry_enabled, m.exit_enabled, m.maintenance_mode, m.updated_at]
             for m in (paper_state, live_state)],
        ))

        # ---- Live wallet snapshot, per venue ----
        gateways = make_gateways() or [BinanceGateway()]
        if v == MODE_LIVE:
            for gw_v in gateways:
                parts.append(f'\n## {gw_v.name} balances (live)\n')
                bals = gw_v.safe_balances()
                if bals is None:
                    parts.append(f'_failed: {gw_v.last_balance_error or "unknown"}_\n')
                    continue
                bal_rows = []
                for wallet in ('spot', 'futures'):
                    w = bals.get(wallet, {}) or {}
                    META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
                    for asset, bal in w.items():
                        if asset in META_KEYS or not isinstance(bal, dict):
                            continue
                        free_q = float(bal.get('free') or 0)
                        used_q = float(bal.get('used') or 0)
                        tot_q = float(bal.get('total') or 0)
                        if free_q == 0 and used_q == 0 and tot_q == 0:
                            continue
                        bal_rows.append([wallet, asset, free_q, used_q, tot_q])
                parts.append(_md_table(['wallet', 'asset', 'free', 'used', 'total'], bal_rows))

        # ---- Equity composition for current view ----
        try:
            breakdown = equity_breakdown(db, gateways, v)
        except Exception as e:
            breakdown = []
            parts.append(f'\n_equity breakdown failed: {e}_\n')
        if breakdown:
            parts.append(f'\n## Equity composition ({v})\n')
            parts.append(_md_table(['bucket', 'value_usdt'], [[b['label'], b['value']] for b in breakdown]))

        # ---- Open positions with leg states ----
        open_positions = db.scalars(select(Position).where(Position.status.in_(OPEN_STATUSES), Position.mode == v)).all()
        parts.append(f'\n## Open positions ({v}) — {len(open_positions)} row(s)\n')
        gateways_by_venue = {g.venue_id: g for g in gateways}
        op_rows = []
        for p in open_positions:
            # Route the leg-state probe through the position's own venue
            # gateway so KuCoin positions get state from KuCoin etc.
            pgw = gateways_by_venue.get(p.exchange)
            if v == MODE_LIVE and pgw is not None:
                try:
                    st = _position_leg_states(pgw, p)
                except Exception:
                    st = {'spot_actual': 0.0, 'perp_actual': 0.0, 'spot_alive': None, 'perp_alive': None, 'spot_min': 0.0, 'perp_min': 0.0}
            else:
                st = {'spot_actual': p.quantity, 'perp_actual': p.quantity, 'spot_alive': True, 'perp_alive': True, 'spot_min': 0.0, 'perp_min': 0.0}
            op_rows.append([
                p.id, p.exchange, p.symbol, p.spot_symbol, p.perp_symbol, p.quantity,
                p.spot_entry_price, p.perp_entry_price,
                st['spot_actual'], st['perp_actual'],
                st['spot_min'], st['perp_min'],
                st['spot_alive'], st['perp_alive'],
                p.last_funding_rate, p.funding_interval_hours,
                p.funding_income_accrued, p.opened_at, p.last_close_error or '',
            ])
        parts.append(_md_table(
            ['id', 'venue', 'symbol', 'spot_symbol', 'perp_symbol', 'qty (base)',
             'spot_entry (USDT)', 'perp_entry (USDT)',
             'spot_actual (base)', 'perp_actual (base)', 'spot_min_lot (base)', 'perp_min_lot (base)',
             'spot_alive', 'perp_alive', 'last_funding_rate', 'interval_h',
             'funding_income (USD)', 'opened_at', 'last_close_error'],
            op_rows,
        ))

        # ---- Closed positions (last 50) ----
        closed_positions = db.scalars(select(Position).where(Position.status == 'closed', Position.mode == v).order_by(desc(Position.id)).limit(50)).all()
        parts.append(f'\n## Closed positions ({v}) — last {len(closed_positions)}\n')
        cp_rows = []
        for c in closed_positions:
            cp_rows.append([
                c.id, c.exchange, c.symbol, c.quantity,
                c.spot_entry_price, c.perp_entry_price,
                position_realized_pnl(db, c), c.funding_income_accrued,
                c.opened_at, c.closed_at,
            ])
        parts.append(_md_table(
            ['id', 'venue', 'symbol', 'qty (base)', 'spot_entry (USDT)', 'perp_entry (USDT)',
             'trade_pnl (USD)', 'funding_income (USD)', 'opened_at', 'closed_at'],
            cp_rows,
        ))

        # ---- Trades (last 100) ----
        trades = db.scalars(select(Trade).where(Trade.mode == v).order_by(desc(Trade.id)).limit(100)).all()
        parts.append(f'\n## Recent trades ({v}) — last {len(trades)}\n')
        parts.append(_md_table(
            ['id', 'ts', 'venue', 'leg', 'position_id', 'symbol', 'side', 'qty (base)', 'price (USDT)', 'fee (USDT)'],
            [[t.id, t.ts, t.exchange, t.venue, t.position_id, t.symbol, t.side, t.quantity, t.price, t.fee] for t in trades],
        ))

        # ---- Capital flows ----
        flows = db.scalars(select(CapitalFlow).where(CapitalFlow.mode == v).order_by(desc(CapitalFlow.id)).limit(100)).all()
        parts.append(f'\n## Capital flows ({v}) — last {len(flows)}\n')
        parts.append(_md_table(
            ['id', 'ts', 'amount (USDT)', 'kind', 'detected_by', 'note'],
            [[f.id, f.ts, f.amount_usdt, f.kind, f.detected_by, f.note] for f in flows],
        ))

        # ---- Bot events (last 100) ----
        events = db.scalars(select(BotEvent).where(BotEvent.mode == v).order_by(desc(BotEvent.id)).limit(100)).all()
        parts.append(f'\n## Bot events ({v}) — last {len(events)}\n')
        parts.append(_md_table(
            ['id', 'ts', 'level', 'message'],
            [[e.id, e.ts, e.level, e.message] for e in events],
        ))

        # ---- Latest scan ----
        latest_scan = db.scalar(select(ScanResult).where(ScanResult.mode == v).order_by(desc(ScanResult.id)).limit(1))
        if latest_scan:
            parts.append(f'\n## Latest scan ({v})\n')
            parts.append(_md_table(
                ['ts', 'candidates_total', 'candidates_passing', 'action', 'note'],
                [[latest_scan.ts, latest_scan.candidates_total, latest_scan.candidates_passing, latest_scan.action, latest_scan.note]],
            ))
            parts.append('\n```json\n' + (latest_scan.top_candidates or '[]') + '\n```\n')

        # ---- Outbound IP ----
        outbound_ip, ip_error = get_outbound_ip()
        parts.append('\n## Network\n')
        parts.append(_md_table(['key', 'value'], [
            ['outbound_ip', outbound_ip or '<unavailable>'],
            ['outbound_ip_error', ip_error or ''],
        ]))

    # ---- Live API probes (re-run, fresh) ----
    parts.append('\n## Exchange API probes\n')
    sections = _gather_exchange_status()
    for section in sections:
        parts.append(f'\n### {section["name"]}\n')
        parts.append(_md_table(['key', 'value'], [
            ['configured', section['configured']],
            ['api_key', section['key_masked']],
            ['api_secret', section['secret_masked']],
            *[[c['label'], c['value']] for c in section.get('extra_creds', [])],
            ['last_balance_error', section.get('last_balance_error', '')],
        ]))
        if section.get('probes'):
            parts.append('\n')
            for probe in section['probes']:
                status_tag = 'OK' if probe['ok'] else 'FAIL'
                parts.append(f'\n#### {status_tag} — {probe["label"]} ({probe["latency_ms"]} ms)\n')
                if probe['ok']:
                    parts.append('```json\n' + _truncate_json(probe['raw'], max_chars=4000) + '\n```\n')
                else:
                    parts.append(f'```\n{probe["err"]}\n```\n')

    return ''.join(parts)


@app.get('/safety', response_class=HTMLResponse)
def safety_page(request: Request, view: str | None = None, refresh_ip: int = 0, view_cookie: str | None = Cookie(default=None, alias='view'), _: None = Depends(auth)):
    v = _resolve_view(view, view_cookie)
    outbound_ip, ip_error = get_outbound_ip(force=bool(refresh_ip))
    with SessionLocal() as db:
        ctx = _shared_ctx(request, v, db)
        ctx['active'] = 'safety'
        ctx['cfg'] = get_strategy_config(db)
        ctx['outbound_ip'] = outbound_ip
        ctx['outbound_ip_error'] = ip_error
    return templates.TemplateResponse(request, 'safety.html', ctx)
