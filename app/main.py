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
    get_all_earn_states,
    get_earn_state,
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
    TRADE_TYPE_LABELS,
    Trade,
)
from app.network import get_outbound_ip
from app.safety import basis_bps

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
            # Bootstrap one EarnState row per venue so the per-(mode, venue)
            # composite key is populated and the dashboard finds something
            # to read on first render. Live venues we know about explicitly
            # plus a 'paper' pseudo-venue for the paper mode display.
            for vid in ('binance', 'kucoin', 'paper'):
                get_earn_state(db, m, exchange=vid)
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
            try:
                earn_usdt, _ = gw.earn_balance_usdt()
                earn_usdt = earn_usdt or 0.0
            except Exception:
                earn_usdt = 0.0
            total += spot_usdt + fut_usdt + spot_assets + earn_usdt
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
    open_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == mode)).all()
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
                elif bals is None and g.last_balance_error:
                    errors.append(f'{g.name}: {g.last_balance_error[:140]}')
            if errors:
                balances_error = ' · '.join(errors)

        trade_realized = total_realized_pnl(db, mode=v)
        funding_income_tracked = total_funding_income(db, mode=v)
        unrealized = _unrealized_for_open(db, gateways, v)
        net_capital, net_capital_meta = net_capital_in(db, mode=v, gateways=gateways)
        flow_count_n = db.scalar(select(func.count(CapitalFlow.id)).where(CapitalFlow.mode == v)) or 0
        xirr_value = portfolio_xirr(db, current_equity, mode=v)
        open_count = db.scalar(select(func.count(Position.id)).where(Position.status == 'open', Position.mode == v)) or 0
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

        stuck_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == v, Position.last_close_error != '')).all()
        ctx['stuck_positions'] = [{'symbol': p.symbol, 'err': p.last_close_error[:160]} for p in stuck_positions]

        # Earn aggregation. ``deployed`` is read live per venue (single
        # source of truth — the API) so the dashboard never shows a value
        # larger than equity due to a stale cache. ``cumulative_yield``
        # comes from the EarnState rows because that's only place we
        # accumulate it. The earlier "126% coverage" bug was the deployed
        # cache drifting above reality after several sweep / refresh
        # races; reading live API removes the drift entirely.
        earn_states = get_all_earn_states(db, v)
        earn_yield_by_venue = {es.exchange: es.cumulative_yield_usdt for es in earn_states}
        earn_err_by_venue = {es.exchange: es.last_error for es in earn_states if es.last_error}
        earn_deployed_by_venue: dict[str, float] = {}
        if v == MODE_LIVE and gateways:
            for gw in gateways:
                try:
                    bal, _ = gw.earn_balance_usdt()
                    earn_deployed_by_venue[gw.venue_id] = bal or 0.0
                except Exception:
                    earn_deployed_by_venue[gw.venue_id] = 0.0
        else:
            # Paper mode: trust the EarnState cache (the bot synthesises yield).
            earn_deployed_by_venue = {es.exchange: es.deployed_usdt for es in earn_states}
        earn_total = sum(earn_deployed_by_venue.values())
        earn_yield_total = sum(earn_yield_by_venue.values())
        ctx['earn'] = {
            'enabled': cfg.earn_enabled,
            'deployed': earn_total,
            'cumulative_yield': earn_yield_total,
            'last_error': '; '.join(earn_err_by_venue.values()),
            'per_venue': {
                vid: {
                    'deployed': earn_deployed_by_venue.get(vid, 0.0),
                    'income': earn_yield_by_venue.get(vid, 0.0),
                }
                for vid in set(earn_deployed_by_venue) | set(earn_yield_by_venue)
            },
        }

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

        breakdown_items = equity_breakdown(db, gateways, v, earn_total)
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
        if v == MODE_LIVE:
            spot_free_total = 0.0
            fut_free_total = 0.0
            for g in gateways:
                bals_for_display = g.safe_balances() or {}
                spot_free_total += float((bals_for_display.get('spot', {}).get('USDT') or {}).get('free') or 0)
                fut_free_total += float((bals_for_display.get('futures', {}).get('USDT') or {}).get('free') or 0)
            ctx['live_spot_free'] = spot_free_total
            ctx['live_fut_free'] = fut_free_total
        else:
            ctx['live_spot_free'] = ctx['live_fut_free'] = None

        # Open positions rows + leg detail (formerly /positions).
        open_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == v)).all()
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
def logs_page(request: Request, view: str | None = None, view_cookie: str | None = Cookie(default=None, alias='view'), _: None = Depends(auth)):
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

        rejected = db.scalars(select(RejectedCandidate).where(RejectedCandidate.mode == v).order_by(desc(RejectedCandidate.id)).limit(50)).all()
        rejected_v = [{'ts': _fmt_ts(r.ts), 'venue': r.exchange, 'symbol': r.symbol, 'reason': r.reason, 'funding_rate': r.funding_rate} for r in rejected]

        trades = db.scalars(select(Trade).where(Trade.mode == v).order_by(desc(Trade.id)).limit(30)).all()
        trades_v = [{'ts': _fmt_ts(t.ts), 'symbol': t.symbol, 'exchange': t.exchange, 'leg': t.venue, 'side': t.side, 'quantity': t.quantity, 'price': t.price, 'fee': t.fee} for t in trades]

        ctx.update({'scans': scans, 'events': events_v, 'rejected': rejected_v, 'trades': trades_v})
    response = templates.TemplateResponse(request, 'logs.html', ctx)
    response.set_cookie('view', v, max_age=60 * 60 * 24 * 365, httponly=False)
    return response


@app.get('/config', response_class=HTMLResponse)
def config_page(request: Request, saved: int = 0, view: str | None = None, view_cookie: str | None = Cookie(default=None, alias='view'), _: None = Depends(auth)):
    v = _resolve_view(view, view_cookie)
    with SessionLocal() as db:
        ctx = _shared_ctx(request, v, db)
        ctx['active'] = 'config'
        ctx['cfg'] = get_strategy_config(db)
        ctx['saved'] = bool(saved)
    return templates.TemplateResponse(request, 'config.html', ctx)


@app.post('/config')
def save_config(
    entry_funding_threshold: float = Form(...),
    exit_funding_threshold: float = Form(...),
    min_24h_quote_volume: float = Form(...),
    stop_loss_pct: float = Form(...),
    max_open_positions: int = Form(...),
    max_trades_per_day: int = Form(...),
    min_position_pct: float = Form(...),
    max_position_pct: float = Form(...),
    max_hold_hours: int = Form(...),
    loop_seconds: int = Form(...),
    paper_slippage_bps: float = Form(...),
    paper_fee_bps: float = Form(...),
    paper_starting_equity: float = Form(...),
    max_entry_basis_bps: float = Form(...),
    max_exit_basis_bps: float = Form(...),
    enforce_hedge_check: int = Form(...),
    delisting_check: int = Form(...),
    earn_enabled: int = Form(...),
    earn_idle_threshold_usdt: float = Form(...),
    earn_paper_apr: float = Form(...),
    auto_transfer_enabled: int = Form(...),
    # auto_rebalance_threshold removed — continuous rebalance was retired
    # in favour of the earn-first model. Field kept on the model for
    # back-compat but not surfaced in the form.
    earn_subscribe_spot_assets: int = Form(0),
    perp_leverage: int = Form(1),         # legacy field — kept for back-compat
    max_perp_leverage: int = Form(1),     # current name; surfaced on /config
    futures_buffer_pct: float = Form(0.20),
    binance_max_bfusd_pct: float = Form(0.20),
    min_order_book_depth_usdt: float = Form(500.0),
    depth_band_bps: float = Form(10.0),
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
        cfg.min_position_pct = min_position_pct
        cfg.max_position_pct = max_position_pct
        cfg.max_hold_hours = max_hold_hours
        cfg.loop_seconds = max(5, loop_seconds)
        cfg.paper_slippage_bps = paper_slippage_bps
        cfg.paper_fee_bps = paper_fee_bps
        cfg.paper_starting_equity = paper_starting_equity
        cfg.max_entry_basis_bps = max_entry_basis_bps
        cfg.max_exit_basis_bps = max_exit_basis_bps
        cfg.enforce_hedge_check = bool(enforce_hedge_check)
        cfg.delisting_check = bool(delisting_check)
        cfg.earn_enabled = bool(earn_enabled)
        cfg.earn_idle_threshold_usdt = earn_idle_threshold_usdt
        cfg.earn_paper_apr = earn_paper_apr
        cfg.auto_transfer_enabled = bool(auto_transfer_enabled)
        cfg.earn_subscribe_spot_assets = bool(earn_subscribe_spot_assets)
        # Take ``max_perp_leverage`` (the current form field) preferentially;
        # ``perp_leverage`` is a legacy field kept for older form submissions.
        new_lev = max(1, max_perp_leverage or perp_leverage or 1)
        cfg.max_perp_leverage = new_lev
        cfg.perp_leverage = new_lev  # keep mirrored so any legacy reader sees the same value
        cfg.futures_buffer_pct = max(0.05, min(1.0, futures_buffer_pct))
        cfg.binance_max_bfusd_pct = max(0.0, min(1.0, binance_max_bfusd_pct))
        cfg.min_order_book_depth_usdt = max(0.0, min_order_book_depth_usdt)
        cfg.depth_band_bps = max(1.0, depth_band_bps)
        db.commit()
    return RedirectResponse(url='/config?saved=1', status_code=303)


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
    if has_creds:
        probes.append(_probe('Spot fetch_balance()', lambda: gw.spot.fetch_balance()))
        probes.append(_probe('Futures fetch_balance()', lambda: gw.futures.fetch_balance()))
        probes.append(_probe('Earn flexible USDT position', lambda: gw._call_sapi((
            'sapiV1GetSimpleEarnFlexiblePosition',
            'sapi_v1_get_simple_earn_flexible_position',
            'sapiGetSimpleEarnFlexiblePosition',
        ), {'asset': 'USDT'})))
        probes.append(_probe('Deposit history (USDT, 10d)', lambda: gw.deposit_history('USDT', lookback_days=10, ttl_seconds=0)))
        probes.append(_probe('Withdrawal history (USDT, 10d)', lambda: gw.withdrawal_history('USDT', lookback_days=10, ttl_seconds=0)))
        probes.append(_probe('Sub-account transfer in (USDT, 10d)', lambda: gw.sub_account_transfer_history('USDT', incoming=True, lookback_days=10, ttl_seconds=0)))
        probes.append(_probe('Sub-account transfer out (USDT, 10d)', lambda: gw.sub_account_transfer_history('USDT', incoming=False, lookback_days=10, ttl_seconds=0)))
        # Alternate sub-transfer endpoint name. Some Binance vintages
        # expose subUserHistory but not subTransferHistory; probe both
        # so we don't miss rows behind a naming difference.
        probes.append(_probe('Sub-account subUserHistory (alt endpoint)', lambda: gw._call_sapi((
            'sapiV1GetSubAccountTransferSubUserHistory',
            'sapi_v1_get_sub_account_transfer_sub_user_history',
            'sapiGetSubAccountTransferSubUserHistory',
        ), {'asset': 'USDT', 'startTime': int((datetime.utcnow() - timedelta(days=10)).timestamp() * 1000)})))
        # Universal-transfer history (raw, no intra-account filter) so the
        # operator can see EVERY row Binance returns and spot the user's
        # deposit even if our parser missed it.
        probes.append(_probe('Universal transfers raw (USDT, 10d, paged)', lambda: gw.spot.fetch_transfers('USDT', since=int((datetime.utcnow() - timedelta(days=10)).timestamp() * 1000)) or []))
        # PM account snapshot — confirms we're talking to a PM-enabled key
        # and surfaces the totalWalletBalance / totalEquity for cross-check.
        probes.append(_probe('PM /papi/v1/account', lambda: gw._papi(gw.spot, ('papiGetAccount', 'papi_get_account'))({}) if gw._papi(gw.spot, ('papiGetAccount', 'papi_get_account')) else 'papiGetAccount not in this ccxt build'))
        probes.append(_probe('Open perp positions', lambda: gw.open_perp_positions_raw()))
        probes.append(_probe('Capital-flow ingest (deposits + withdrawals + sub-transfers, 365d)', lambda: {
            'rows': gw.list_capital_flow_records(lookback_days=10),
            'errors': gw.last_history_errors,
        }))
        # Capital subtotal: cash + spot assets + futures + earn (all USDT-denominated).
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
        earn_usdt, _ = gw.earn_balance_usdt()
        earn_usdt = earn_usdt or 0.0
        capital_breakdown = [
            {'label': 'Spot · USDT', 'value': spot_usdt},
            {'label': 'Spot · assets', 'value': spot_assets},
            {'label': 'Futures · USDT', 'value': fut_usdt},
            {'label': 'Earn · USDT', 'value': earn_usdt},
        ]
        capital_subtotal = spot_usdt + spot_assets + fut_usdt + earn_usdt
    bn_account_label = bn_account_detail = ''
    if has_creds:
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
        'extra_creds': [{'label': 'Account type (live)', 'value': f'{bn_account_label} — {bn_account_detail}' if bn_account_label else '<not probed>'}] if has_creds else [],
        'probes': probes,
        'last_balance_error': gw.last_balance_error,
        'capital_subtotal_usdt': capital_subtotal,
        'capital_breakdown': capital_breakdown,
        'role': 'spot + USDM-perp arb (active)',
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
    if kc_configured:
        kgw = KuCoinGateway()
        kc_probes.append(_probe('Spot fetch_balance(trade)', lambda: kgw.spot.fetch_balance({'type': 'trade'})))
        kc_probes.append(_probe('Spot fetch_balance(main)', lambda: kgw.spot.fetch_balance({'type': 'main'})))
        kc_probes.append(_probe('Futures fetch_balance()', lambda: kgw.futures.fetch_balance()))
        kc_probes.append(_probe('Funding rates (markets-derived)', lambda: kgw.funding_rates_dict()))
        kc_probes.append(_probe('Open perp positions', lambda: kgw.open_perp_positions_raw()))
        kc_probes.append(_probe('Deposit history (USDT, 10d)', lambda: kgw.spot.fetch_deposits('USDT', since=int((datetime.utcnow() - timedelta(days=10)).timestamp() * 1000))))
        kc_probes.append(_probe('Withdrawal history (USDT, 10d)', lambda: kgw.spot.fetch_withdrawals('USDT', since=int((datetime.utcnow() - timedelta(days=10)).timestamp() * 1000))))
        kc_probes.append(_probe('Universal transfers raw (USDT, 10d)', lambda: kgw.spot.fetch_transfers('USDT', since=int((datetime.utcnow() - timedelta(days=10)).timestamp() * 1000))))
        # UTA account-mode probe — surfaces the live account state so the
        # operator can confirm the API key sees UTA/Classic correctly.
        kc_probes.append(_probe('Account mode (UTA check)', lambda: {'is_uta_enabled': kgw.spot.is_uta_enabled()}))
        kc_probes.append(_probe('Capital-flow ingest (deposits + sub-transfers)', lambda: {
            'rows': kgw.list_capital_flow_records(lookback_days=10),
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
        # KuCoin's "earn" surface is the funding (main) wallet — that's
        # where auto-lend / Pool-X draws idle USDT from. Pull it via the
        # gateway so the cached balance dict is reused (no extra round-trip).
        kc_earn_usdt, _ = kgw.earn_balance_usdt()
        kc_earn_usdt = kc_earn_usdt or 0.0
        kc_capital_breakdown = [
            {'label': 'Spot · USDT (trade wallet)', 'value': kc_spot_usdt},
            {'label': 'Spot · assets', 'value': kc_spot_assets},
            {'label': 'Futures · USDT (contract wallet)', 'value': kc_fut_usdt},
            {'label': 'Earn · USDT (main / funding wallet)', 'value': kc_earn_usdt},
        ]
        kc_capital_subtotal = kc_spot_usdt + kc_spot_assets + kc_fut_usdt + kc_earn_usdt
    kc_account_label = kc_account_detail = ''
    if kc_configured:
        try:
            kc_account_label, kc_account_detail = kgw.account_type()
        except Exception as e:
            kc_account_label, kc_account_detail = 'Unknown', f'probe error: {str(e)[:80]}'
    kc_extra = [{'label': 'Passphrase', 'value': _mask(kc_pass)}]
    if kc_configured:
        kc_extra.append({'label': 'Account type (live)', 'value': f'{kc_account_label} — {kc_account_detail}'})
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
        'role': 'spot + USDT-perp arb' + (' (active)' if kc_configured else ' (set credentials to activate)'),
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

    # ---- Onchain (future — DEX perps + lending) -----------------------
    # Hyperliquid, Drift, Aevo, GMX-class venues let us trade perps with
    # collateral that simultaneously earns yield (USDC.e on Aave, sUSDe,
    # etc.) — exactly the "yield + liquidation buffer" combo the user
    # asked about. Wallet keys + RPC endpoint go here once we pick a
    # primary protocol. Placeholder section so the maintenance UI shows
    # the venue is on the roadmap.
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
    sharing. Includes: configuration, mode/earn states, open + closed
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
        # Aggregate across venues so the export captures the full pool,
        # not just one venue's slice.
        paper_earn_states = get_all_earn_states(db, MODE_PAPER)
        live_earn_states = get_all_earn_states(db, MODE_LIVE)
        paper_earn_deployed = sum(es.deployed_usdt for es in paper_earn_states)
        paper_earn_yield = sum(es.cumulative_yield_usdt for es in paper_earn_states)
        live_earn_deployed = sum(es.deployed_usdt for es in live_earn_states)
        live_earn_yield = sum(es.cumulative_yield_usdt for es in live_earn_states)

        # ---- Configuration ----
        parts.append('## Strategy configuration\n')
        cfg_rows = [
            ['entry_funding_threshold', cfg.entry_funding_threshold],
            ['exit_funding_threshold', cfg.exit_funding_threshold],
            ['min_24h_quote_volume', cfg.min_24h_quote_volume],
            ['min_order_book_depth_usdt', cfg.min_order_book_depth_usdt],
            ['depth_band_bps', cfg.depth_band_bps],
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
            ['max_entry_basis_bps', cfg.max_entry_basis_bps],
            ['max_exit_basis_bps', cfg.max_exit_basis_bps],
            ['enforce_hedge_check', cfg.enforce_hedge_check],
            ['delisting_check', cfg.delisting_check],
            ['earn_enabled', cfg.earn_enabled],
            ['earn_idle_threshold_usdt', cfg.earn_idle_threshold_usdt],
            ['earn_paper_apr', cfg.earn_paper_apr],
            ['earn_subscribe_spot_assets', cfg.earn_subscribe_spot_assets],
            ['auto_transfer_enabled', cfg.auto_transfer_enabled],
            ['perp_leverage', cfg.perp_leverage],
        ]
        parts.append(_md_table(['key', 'value'], cfg_rows))

        # ---- Mode + earn states ----
        parts.append('\n## Mode states\n')
        parts.append(_md_table(
            ['mode', 'entry_enabled', 'exit_enabled', 'maintenance_mode', 'updated_at'],
            [[m.mode, m.entry_enabled, m.exit_enabled, m.maintenance_mode, m.updated_at]
             for m in (paper_state, live_state)],
        ))
        parts.append('\n## Earn states (per mode + venue)\n')
        parts.append(_md_table(
            ['mode', 'venue', 'deployed_usdt', 'cumulative_yield_usdt', 'last_accrual_ts', 'last_error'],
            [[e.mode, e.exchange, e.deployed_usdt, e.cumulative_yield_usdt, e.last_accrual_ts, e.last_error or '']
             for e in (*paper_earn_states, *live_earn_states)],
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
        earn_deployed_for_view = live_earn_deployed if v == MODE_LIVE else paper_earn_deployed
        try:
            breakdown = equity_breakdown(db, gateways, v, earn_deployed_for_view)
        except Exception as e:
            breakdown = []
            parts.append(f'\n_equity breakdown failed: {e}_\n')
        if breakdown:
            parts.append(f'\n## Equity composition ({v})\n')
            parts.append(_md_table(['bucket', 'value_usdt'], [[b['label'], b['value']] for b in breakdown]))

        # ---- Open positions with leg states ----
        open_positions = db.scalars(select(Position).where(Position.status == 'open', Position.mode == v)).all()
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
