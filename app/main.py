from datetime import datetime

from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import desc, select

from app.bot import get_runtime_state, get_strategy_config, run_loop
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.exchange import BinanceGateway
from app.models import BotEvent, EquityCurve, Position, RejectedCandidate, RuntimeState, Trade

app = FastAPI(title='Funding Arb Bot')
security = HTTPBasic()


def auth(creds: HTTPBasicCredentials = Depends(security)):
    if creds.username != settings.dashboard_user or creds.password != settings.dashboard_password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@app.on_event('startup')
def startup() -> None:
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        get_runtime_state(db)
        get_strategy_config(db)
        db.commit()


@app.get('/health')
def health():
    return {'ok': True}


@app.post('/run-once')
def run_once(_: None = Depends(auth)):
    run_loop()
    return {'status': 'finished'}


@app.post('/mode')
def set_mode(mode: str = Form(...), confirm: str = Form(''), _: None = Depends(auth)):
    with SessionLocal() as db:
        state = db.scalar(select(RuntimeState).where(RuntimeState.id == 1))
        if state is None:
            state = RuntimeState(id=1, paper_mode=True, maintenance_mode=False)
            db.add(state)
        if mode == 'live':
            if confirm != 'LIVE':
                raise HTTPException(status_code=400, detail='Type LIVE in confirm box to switch to live mode.')
            state.paper_mode = False
        elif mode == 'paper':
            state.paper_mode = True
        db.commit()
    return RedirectResponse(url='/dashboard', status_code=303)


@app.post('/config')
def set_config(
    min_yield: float = Form(...),
    exit_yield: float = Form(...),
    min_volume: float = Form(...),
    max_hold_hours: int = Form(...),
    max_open_positions: int = Form(...),
    max_trades_per_day: int = Form(...),
    max_position_notional: float = Form(...),
    min_symbol_notional: float = Form(...),
    injected_capital_usdt: float = Form(...),
    _: None = Depends(auth),
):
    with SessionLocal() as db:
        cfg = get_strategy_config(db)
        cfg.min_yield = min_yield
        cfg.exit_yield = exit_yield
        cfg.min_volume = min_volume
        cfg.max_hold_hours = max_hold_hours
        cfg.max_open_positions = max_open_positions
        cfg.max_trades_per_day = max_trades_per_day
        cfg.max_position_notional = max_position_notional
        cfg.min_symbol_notional = min_symbol_notional
        cfg.injected_capital_usdt = injected_capital_usdt
        db.commit()
    return RedirectResponse(url='/dashboard#config', status_code=303)


def xirr_like(days: int, pnl: float, capital: float) -> float:
    if capital <= 0 or days <= 0:
        return 0.0
    return ((1 + pnl / capital) ** (365 / days) - 1) if pnl > -capital else -1.0


@app.get('/dashboard', response_class=HTMLResponse)
def dashboard(_: None = Depends(auth)):
    with SessionLocal() as db:
        positions = db.scalars(select(Position).where(Position.status == 'open')).all()
        eq = db.scalars(select(EquityCurve).order_by(desc(EquityCurve.ts)).limit(200)).all()
        trades = db.scalars(select(Trade).order_by(desc(Trade.ts)).limit(50)).all()
        rejected = db.scalars(select(RejectedCandidate).order_by(desc(RejectedCandidate.ts)).limit(100)).all()
        events = db.scalars(select(BotEvent).order_by(desc(BotEvent.ts)).limit(200)).all()
        state = get_runtime_state(db)
        cfg = get_strategy_config(db)

    gw = BinanceGateway()
    balances = gw.balances()
    mode_label = 'PAPER' if state.paper_mode else 'LIVE'
    mode_class = 'bad' if mode_label == 'LIVE' else 'ok'

    spot_usdt = float((balances['spot'].get('USDT') or {}).get('total') or 0)
    fut_usdt = float((balances['futures'].get('USDT') or {}).get('total') or 0)
    equity_now = spot_usdt + fut_usdt
    pnl = equity_now - cfg.injected_capital_usdt
    start_ts = eq[-1].ts if eq else datetime.utcnow()
    days = max((datetime.utcnow() - start_ts).days, 1)
    annual = xirr_like(days, pnl, cfg.injected_capital_usdt)
    eq_points = list(reversed(eq))[:60]
    eq_labels = [e.ts.strftime('%m-%d %H:%M') for e in eq_points]
    eq_values = [round(e.equity_usdt, 6) for e in eq_points]

    pos_rows = ''.join(f'<tr><td>{p.symbol}</td><td>{p.quantity:.6f}</td><td>{p.entry_funding_rate:.5%}</td><td>{p.last_funding_rate:.5%}</td><td>{p.opened_at}</td></tr>' for p in positions)
    tr_rows = ''.join(f'<tr><td>{t.ts}</td><td>{t.symbol}</td><td>{t.venue}</td><td>{t.side}</td><td>{t.quantity:.6f}</td><td>{t.price:.6f}</td><td>{t.fee:.6f}</td></tr>' for t in trades)
    rej_rows = ''.join(f'<tr><td>{r.ts}</td><td>{r.symbol}</td><td>{r.reason}</td><td>{r.funding_rate:.5%}</td></tr>' for r in rejected)
    event_rows = ''.join(f'<tr><td>{e.ts}</td><td>{e.level}</td><td>{e.message}</td></tr>' for e in events)

    return f"""
    <html><head><title>Funding Arb Control Center</title><script src='https://cdn.jsdelivr.net/npm/chart.js'></script>
    <style>
    body {{ font-family: Inter, Arial, sans-serif; margin: 0; background:#0f172a; color:#e2e8f0; }}
    .wrap {{ max-width:1200px; margin:20px auto; padding:0 16px; }}
    .card {{ background:#111827; border:1px solid #334155; border-radius:12px; padding:16px; margin-bottom:16px; }}
    .kpi {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }}
    .kpi div {{ background:#1e293b; padding:10px; border-radius:8px; }}
    table {{ width:100%; border-collapse:collapse; font-size:13px; }}
    th,td {{ border-bottom:1px solid #334155; padding:8px; text-align:left; }}
    .tabs a {{ color:#93c5fd; margin-right:14px; text-decoration:none; }}
    input,button {{ padding:8px; border-radius:6px; border:1px solid #334155; background:#0b1220; color:#e2e8f0; }}
    button {{ background:#1d4ed8; }}
    .ok{{color:#22c55e;}} .bad{{color:#ef4444;}}
    </style></head><body><div class='wrap'>
    <h1>Funding Arb Control Center</h1>
    <div class='tabs'><a href='#overview'>Overview</a><a href='#positions'>Open Positions</a><a href='#logs'>Logs & Scans</a><a href='#config'>Configuration</a><a href='#safety'>Safety Rules</a></div>

    <div id='overview' class='card'>
      <h2>Portfolio & PnL Overview</h2>
      <div class='kpi'>
        <div><b>Mode</b><br><span class='{mode_class}'>{mode_label}</span></div>
        <div><b>Injected Capital</b><br>{cfg.injected_capital_usdt:.2f} USDT</div>
        <div><b>Equity Now</b><br>{equity_now:.2f} USDT</div>
        <div><b>PnL</b><br>{pnl:.2f} USDT ({annual:.2%} annualized)</div>
      </div>
      <p>Spot USDT: {spot_usdt:.2f} | Futures USDT: {fut_usdt:.2f}</p>
      <div class='card'><h3>Portfolio Breakdown</h3><canvas id='portfolioChart' height='110'></canvas></div>
      <div class='card'><h3>Equity Curve</h3><canvas id='equityChart' height='110'></canvas></div>
      <form action='/mode' method='post'>
        <button type='submit' name='mode' value='paper'>Switch to PAPER</button>
        <button type='submit' name='mode' value='live'>Switch to LIVE</button>
        <input name='confirm' placeholder='Type LIVE to confirm'>
      </form>
    </div>

    <div id='positions' class='card'><h2>Open Positions</h2><table><tr><th>Symbol</th><th>Qty</th><th>Entry Funding</th><th>Current Funding</th><th>Opened</th></tr>{pos_rows}</table></div>
    <div class='card'><h2>Recent Trades</h2><table><tr><th>Time</th><th>Symbol</th><th>Venue</th><th>Side</th><th>Qty</th><th>Price</th><th>Fee</th></tr>{tr_rows}</table></div>

    <div id='logs' class='card'><h2>Logs & Scan Opportunities</h2><table><tr><th>Time</th><th>Level</th><th>Message</th></tr>{event_rows}</table>
    <h3>Rejected Candidates</h3><table><tr><th>Time</th><th>Symbol</th><th>Reason</th><th>Funding</th></tr>{rej_rows}</table></div>

    <div id='config' class='card'><h2>Configuration</h2>
    <form action='/config' method='post'>
      <p>Min Yield <input name='min_yield' value='{cfg.min_yield}'></p>
      <p>Exit Yield <input name='exit_yield' value='{cfg.exit_yield}'></p>
      <p>Min Volume <input name='min_volume' value='{cfg.min_volume}'></p>
      <p>Max Hold Hours <input name='max_hold_hours' value='{cfg.max_hold_hours}'></p>
      <p>Max Open Positions <input name='max_open_positions' value='{cfg.max_open_positions}'></p>
      <p>Max Trades/Day <input name='max_trades_per_day' value='{cfg.max_trades_per_day}'></p>
      <p>Max Position Notional <input name='max_position_notional' value='{cfg.max_position_notional}'></p>
      <p>Min Symbol Notional <input name='min_symbol_notional' value='{cfg.min_symbol_notional}'></p>
      <p>Injected Capital USDT <input name='injected_capital_usdt' value='{cfg.injected_capital_usdt}'></p>
      <button type='submit'>Save Configuration</button>
    </form></div>

    <div id='safety' class='card'><h2>Safety Checks & Live Rules</h2>
    <ul>
      <li>Runtime mode currently: <b class='{mode_class}'>{mode_label}</b>.</li>
      <li>LIVE mode requires explicit 'LIVE' confirmation.</li>
      <li>Position caps: max open {cfg.max_open_positions}, max trades/day {cfg.max_trades_per_day}.</li>
      <li>Notional guards: max position {cfg.max_position_notional}, min symbol {cfg.min_symbol_notional}.</li>
      <li>Funding gates: entry {cfg.min_yield:.5%}, exit {cfg.exit_yield:.5%}.</li>
    </ul></div>
    </div>
    <script>
    new Chart(document.getElementById('portfolioChart'), {{
      type: 'doughnut',
      data: {{ labels: ['Spot USDT','Futures USDT'], datasets: [{{ data: [{spot_usdt:.6f}, {fut_usdt:.6f}], backgroundColor:['#38bdf8','#818cf8'] }}] }},
      options: {{ plugins: {{ legend: {{ labels: {{ color: '#e2e8f0'}} }} }} }}
    }});
    new Chart(document.getElementById('equityChart'), {{
      type: 'line',
      data: {{ labels: {eq_labels}, datasets: [{{ label:'Equity USDT', data: {eq_values}, borderColor:'#22c55e', tension:0.2 }}] }},
      options: {{ scales: {{ x: {{ ticks:{{ color:'#94a3b8'}}}}, y:{{ ticks:{{ color:'#94a3b8'}}}} }}, plugins:{{ legend:{{ labels:{{ color:'#e2e8f0'}}}} }} }}
    }});
    </script>
    </body></html>
    """
