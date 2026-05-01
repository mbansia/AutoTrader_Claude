# AutoTrader Codex - Binance Funding Arbitrage

## Minimal required `.env`
Only these variables are required:

```env
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=your_password
```

All strategy/risk defaults are coded in `app/config.py` and can be changed in code/chat-driven updates.

## Coolify deployment
Create two applications from the same repo:

1. **API app**
   - Start command: `uvicorn app.main:app --host 0.0.0.0 --port 8000`
   - Port: `8000`
   - Env vars: only the 4 above

2. **Worker app**
   - Start command: `python -c "from app.bot import run_loop; run_loop()"`
   - Env vars: same 4 vars

## Safety
- Default runtime starts in PAPER mode via coded defaults.
- Switch to LIVE from dashboard only when ready.
