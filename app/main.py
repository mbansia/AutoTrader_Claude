from fastapi import Depends, FastAPI, Form, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import desc, select

from app.bot import get_runtime_state, run_loop
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.exchange import BinanceGateway
from app.models import EquityCurve, Position, RejectedCandidate, RuntimeState, Trade

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


@app.get('/dashboard', response_class=HTMLResponse)
def dashboard(_: None = Depends(auth)):
    with SessionLocal() as db:
        positions = db.scalars(select(Position).where(Position.status == 'open')).all()
        eq = db.scalars(select(EquityCurve).order_by(desc(EquityCurve.ts)).limit(50)).all()
        trades = db.scalars(select(Trade).order_by(desc(Trade.ts)).limit(20)).all()
        rejected = db.scalars(select(RejectedCandidate).order_by(desc(RejectedCandidate.ts)).limit(30)).all()
        state = get_runtime_state(db)
    gw = BinanceGateway()
    balances = gw.balances()
    rows = ''.join(f'<tr><td>{p.symbol}</td><td>{p.quantity:.6f}</td><td>{p.entry_funding_rate:.5%}</td><td>{p.last_funding_rate:.5%}</td></tr>' for p in positions)
    rej_rows = ''.join(f'<tr><td>{r.symbol}</td><td>{r.reason}</td><td>{r.funding_rate:.5%}</td></tr>' for r in rejected)
    tr_rows = ''.join(f'<tr><td>{t.ts}</td><td>{t.symbol}</td><td>{t.venue}</td><td>{t.side}</td><td>{t.quantity:.6f}</td><td>{t.price:.6f}</td></tr>' for t in trades)
    eq_last = eq[0].equity_usdt if eq else 0
    mode_label = 'PAPER' if state.paper_mode else 'LIVE'
    return f"""
    <html><body><h1>Funding Arb Dashboard</h1>
    <h2>Mode: {mode_label}</h2>
    <form action='/mode' method='post'>
      <button type='submit' name='mode' value='paper'>Switch to PAPER</button>
      <button type='submit' name='mode' value='live'>Switch to LIVE</button>
      <input name='confirm' placeholder='Type LIVE to confirm'>
    </form>
    <h2>Equity (latest): {eq_last:.4f} USDT</h2>
    <h3>Live account panel</h3><pre>{balances}</pre>
    <h3>Open positions</h3><table border='1'><tr><th>Symbol</th><th>Qty</th><th>Entry Funding</th><th>Current Funding</th></tr>{rows}</table>
    <h3>Recent trades</h3><table border='1'><tr><th>Time</th><th>Symbol</th><th>Venue</th><th>Side</th><th>Qty</th><th>Price</th></tr>{tr_rows}</table>
    <h3>Rejected candidates</h3><table border='1'><tr><th>Symbol</th><th>Reason</th><th>Funding</th></tr>{rej_rows}</table>
    </body></html>
    """
