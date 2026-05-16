# AutoTrader Codex — funding-rate arbitrage bot

SSOT: [`docs/SYSTEM.md`](docs/SYSTEM.md). Operator preferences:
[`CLAUDE.md`](CLAUDE.md).

## Stack

Python 3.11+, FastAPI + Jinja2 (server-rendered, no JS build step),
SQLAlchemy 2.x on SQLite, ccxt for venue APIs. Single-process; one cycle
thread per (mode, gateway). Every persisted datetime is UTC-aware.

## Layout

Two parallel application packages live in the repo during the v1.3 → v1.4
cutover (see §17 of `docs/SYSTEM.md`):

| Package | Status | Entry |
|---|---|---|
| `app/` | **v1.3** running production (until cutover). | `uvicorn app.main:app` |
| `core/` `state/` `gateways/` `loop/` `diagnostics/` `web/` | **v1.4** spec-conformant rewrite. | `uvicorn web.app:app` |

The v1.4 packages share NO code with `app/`. Cutover is a single Coolify
start-command change; rollback is the reverse.

## Required environment

```env
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
KUCOIN_API_KEY=...
KUCOIN_API_SECRET=...
KUCOIN_PASSPHRASE=...
HYPERLIQUID_WALLET_ADDRESS=0x...     # v1.5 — EVM auth (no API key)
HYPERLIQUID_PRIVATE_KEY=0x...        # v1.5 — guard like any secret
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=your_password
DIAGNOSTICS_TOKEN=your_token
BINANCE_EXPECTED_ACCOUNT_ID=...      # v1.4 — boot-time assertion
KUCOIN_EXPECTED_ACCOUNT_ID=...       # v1.4 — boot-time assertion
HYPERLIQUID_EXPECTED_ACCOUNT_ID=...  # v1.5 — defaults to wallet address
DATABASE_URL=sqlite:////app/data/bot.db   # Coolify persistent volume
```

Every UI route except `/health` and `/api/diagnostics` requires HTTP Basic.
`/api/diagnostics` is `?token=...` matched against `DIAGNOSTICS_TOKEN`;
returns 503 if the env var is unset (refuses to be silently public).

## Running

```bash
# v1.4 (post-cutover):
uvicorn web.app:app --host 0.0.0.0 --port 8000

# v1.3 (legacy, until cutover):
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Tests

```bash
python -m pytest tests/
```

106 tests cover the v1.4 core: gate math + APY + basis (§3.1 math),
closed-form sizing per side (buy/sell/perp), position state machine,
paper-mode end-to-end cycle (entries, exits, naked-direction recovery,
maintenance, crash isolation, single-leg orphan with + without rollback,
deferral sign correctness), diagnostics JSON shape and anomaly rules,
HTML route auth contract, config form round-trip, gateway protocol
surface, ccxt helper utilities. Live venue integration tests (`BinanceGateway`,
`KuCoinGateway` against real APIs) are §17 Stage 5 work.

## Parity harness — pre-cutover sanity check

Two modes. **In-process** is the simplest path when only v1.3 is deployed:

```bash
# 1. Grab a snapshot of the production SQLite DB from Coolify
#    (Coolify shell → cat /app/data/bot.db | base64, or scp the volume).
scp coolify-host:/var/lib/docker/volumes/<volume>/_data/bot.db ./prod_bot.db

# 2. Run the harness. The script boots v1.5 in-process against a
#    read-only copy of the snapshot — no trading, no DB mutation.
python scripts/diagnostics_parity.py \
    --legacy "https://your-bot.coolify.app/api/diagnostics?token=$DIAGNOSTICS_TOKEN" \
    --in-process \
    --db-snapshot ./prod_bot.db \
    --token "$DIAGNOSTICS_TOKEN"

# Exit 0 = parity OK. Exit 1 = drift + structural diff printed. Exit 2 = fetch error.
# Add --output diff.json to capture the full diff for triage.
```

When both v1.3 and v1.5 are deployed (side-by-side validation window):

```bash
python scripts/diagnostics_parity.py \
    --legacy "https://prod-v1.3.app/api/diagnostics?token=$TOK" \
    --new    "https://staging-v1.5.app/api/diagnostics?token=$TOK"
```

**Caveat:** the harness compares the diagnostics SHAPE, not behavioral
decisions. A clean-pass means the v1.5 endpoint returns identically-
structured data to v1.3's; it does NOT confirm both bots would make the
same trade decisions on the same inputs. For that you need 48h of
side-by-side paper-mode running (§17 Stage 5).

## Activating Hyperliquid

HL is opt-in (default off). To turn it on:

1. **Set env vars in Coolify:**
   ```
   HYPERLIQUID_WALLET_ADDRESS=0x...      # master wallet address (holds USDC)
   HYPERLIQUID_PRIVATE_KEY=0x...         # API/agent wallet private key — NOT the master key
   HYPERLIQUID_EXPECTED_ACCOUNT_ID=0x... # = wallet address; boot-time assertion
   ACTIVE_EXCHANGES=binance,kucoin,hyperliquid
   ```

   **`HYPERLIQUID_PRIVATE_KEY` must be the agent wallet's key, NOT the
   master wallet's.** Hyperliquid's UI lets you authorise an "API wallet"
   (agent) that can place orders but NOT withdraw funds. If the env var
   leaks, an attacker with the agent key cannot drain the pool.

2. **Read `docs/SYSTEM.md` §6.4 first.** Hyperliquid pays funding HOURLY
   (vs 4h or 8h on the CEXes). The same per-window funding rate
   compounds to ~8× the APY on HL. **You should usually set a higher
   `entry_min_net_apy` for the `hyperliquid_same_venue_funding_arb`
   strategy** so the bot doesn't enter on rates that decay too fast to
   recoup round-trip fees. The `/config?strategy=hyperliquid_same_venue_funding_arb`
   tab in the UI lets you tune this independently of the CEX strategies.

3. **Deploy.** On startup, the runner asserts that the venue reports the
   expected account id (the wallet address) and refuses to start the
   live worker on mismatch — catches env-var typos before any orders fire.

The legacy v1.3 bot in `app/` does NOT know about Hyperliquid. You can
only activate HL **after** the start-command cutover from
`uvicorn app.main:app` to `uvicorn web.app:app`.

## Cutover runbook (§17 Stage 6)

The v1.5 app is **runnable** — `web/app.py`'s `lifespan` boots a
background loop runner (`loop.runner`) that spawns one thread per
`(mode, exchange)`. Pre-v1.5 the new app had no loop and would have
been silent in production; that gap is now closed.

To cut over:

1. **Provision env vars** in Coolify if not already set: `DASHBOARD_USER`,
   `DASHBOARD_PASSWORD`, `DIAGNOSTICS_TOKEN`, `BINANCE_API_KEY/SECRET`,
   `BINANCE_EXPECTED_ACCOUNT_ID`, `KUCOIN_API_KEY/SECRET/PASSPHRASE`,
   `KUCOIN_EXPECTED_ACCOUNT_ID`, `BOT_WORKER_ENABLED=1`, and the
   `DATABASE_URL` already pointing at the persistent volume.
2. **Deploy v1.5 alongside v1.3** on a different port, set
   `BOT_WORKER_ENABLED=0` on the v1.5 instance so it serves the UI but
   does not trade.
3. **Run the parity harness for 48 hours** (paper-mode):
   `python scripts/diagnostics_parity.py --legacy ... --new ...`. Exit 0
   = clean.
4. **Atomic cutover**: change Coolify start command on the production
   service from `uvicorn app.main:app` to `uvicorn web.app:app`. Set
   `BOT_WORKER_ENABLED=1` (default). Coolify replaces the container in
   one swap — no overlap window, no double-bot writes to the same DB.
5. **Keep v1.3 deployable for 7 days** as fallback — do not delete the
   `app/` package. Roll back = swap start command back.
6. **After 7 days clean**: archive `app/`.

To activate Hyperliquid (default-off even with creds):
`ACTIVE_EXCHANGES=binance,kucoin,hyperliquid` + the HL env vars. Read
§6.4 first — hourly funding implies the `entry_min_net_apy` target on
HL should usually be set higher than on CEX 8h venues.

## Safety / never-list

`docs/SYSTEM.md` §11 + `CLAUDE.md` are binding:

- Never push directly to `main` (use PRs via MCP).
- Never `--no-verify`, force-push, or destructive shell.
- Never touch venue credentials in code or DB.
- Every behavior-changing PR updates `docs/SYSTEM.md` in the same commit.
