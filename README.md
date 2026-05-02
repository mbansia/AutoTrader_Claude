# AutoTrader Codex - Binance Funding Arbitrage

## Minimal required `.env`
Only these variables are required:

```env
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=your_password
```

Optional:
```env
BOT_WORKER_ENABLED=1   # default; set to 0 if you want to run the worker as a separate process
```

All strategy/risk parameters are now editable from the **Configuration** tab in the dashboard and persist in the local SQLite DB.

## Coolify deployment

The simplest setup is **one app**: the API process auto-starts the bot loop in a background thread.

- Start command: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1`
- Port: `8000`
- Env vars: the 4 above
- Persist `./bot.db` across deploys (mount it on a volume)

Note: keep `--workers 1` so only one bot loop runs against the SQLite DB. If you want to scale the API horizontally, set `BOT_WORKER_ENABLED=0` on every API replica and run a single dedicated worker app:

- Start command: `python -c "from app.bot import run_loop; run_loop()"`

## Safety
- Default runtime starts in PAPER mode via coded defaults.
- Switch to LIVE from the dashboard only when ready (requires typing `LIVE` to confirm).
- Stop-loss, max hold, daily trade cap, and entry/exit kill switches are all editable from the Configuration tab.
- The Safety & Rules tab shows every guardrail currently in effect.
