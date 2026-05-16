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

## Required environment (only secrets + DB pointer)

```env
# Auth — required, must be env (you can't store the dashboard's own
# password inside the dashboard).
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=your_password
DIAGNOSTICS_TOKEN=your_token

# Venue credentials — required for each venue you want to use.
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
KUCOIN_API_KEY=...
KUCOIN_API_SECRET=...
KUCOIN_PASSPHRASE=...
HYPERLIQUID_WALLET_ADDRESS=0x...   # master wallet (holds USDC)
HYPERLIQUID_PRIVATE_KEY=0x...      # AGENT wallet key — never the master key

# DB pointer — required.
DATABASE_URL=sqlite:////app/data/bot.db
```

That's everything you need to set in env. **Per-venue activation +
account-id assertion now live in the dashboard** at `/safety` — no env
vars needed. First boot seeds the DB with `binance` and `kucoin` active
by default; `hyperliquid` inactive (opt-in via the dashboard toggle).

Every UI route except `/health` and `/api/diagnostics` requires HTTP Basic.
`/api/diagnostics` is `?token=...` matched against `DIAGNOSTICS_TOKEN`;
returns 503 if the env var is unset (refuses to be silently public).

Optional env overrides (you almost certainly don't need them):

- `BOT_WORKER_ENABLED=0` — UI-only mode. Defaults to on. Used by the
  parity harness's in-process flow.
- `ACTIVE_EXCHANGES=binance,kucoin,hyperliquid` — **deprecated** v1.6
  back-compat shim. If set, mirrors into the DB once on next boot. Use
  the `/safety` dashboard going forward.
- `BINANCE_EXPECTED_ACCOUNT_ID` / `KUCOIN_EXPECTED_ACCOUNT_ID` /
  `HYPERLIQUID_EXPECTED_ACCOUNT_ID` — **deprecated** v1.6. The expected
  account id lives on `venue_state.expected_account_id` in the DB and is
  edited via `/safety`. Env values still work but DB takes precedence
  when both are set.

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

HL is opt-in. Three steps, two of them in the dashboard:

1. **Set the two HL secrets in env** (the only HL config that needs to
   be in env):

   ```
   HYPERLIQUID_WALLET_ADDRESS=0x...   # master wallet address (holds USDC)
   HYPERLIQUID_PRIVATE_KEY=0x...      # AGENT wallet private key — NOT master
   ```

   `HYPERLIQUID_PRIVATE_KEY` **must** be the agent wallet's key from
   Hyperliquid's API page, never the master wallet's. The agent key can
   trade but not withdraw — leaking it doesn't drain the pool.

2. **In `/safety` → Venues**, check **Active** on the `hyperliquid` row.
   Paste your wallet address into **Expected account id** and click Save.

3. **In `/config?strategy=hyperliquid_same_venue_funding_arb`**, set a
   higher `entry_min_net_apy` than for the CEX strategies. HL pays
   funding HOURLY — the same per-window rate compounds to ~8× the APY of
   a Binance 8h pair, so positions decay much faster (per `docs/SYSTEM.md`
   §6.4). Default 20% will work but you should review.

On the next cycle, the runner reads `venue_state.active = true`,
constructs the live `HyperliquidGateway` from the env secrets, asserts
that the venue reports your wallet address (refuses to start on
mismatch), registers the gateway, and spawns paper + live worker threads.

The legacy v1.3 bot in `app/` does NOT know about Hyperliquid. HL
activation only works after the start-command cutover from
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
