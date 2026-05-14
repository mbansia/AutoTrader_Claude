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
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=your_password
DIAGNOSTICS_TOKEN=your_token
BINANCE_EXPECTED_ACCOUNT_ID=...      # v1.4 — boot-time assertion
KUCOIN_EXPECTED_ACCOUNT_ID=...       # v1.4 — boot-time assertion
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

## Parity harness

```bash
python scripts/diagnostics_parity.py \
    --legacy "http://localhost:8000/api/diagnostics?token=$TOK" \
    --new    "http://localhost:8001/api/diagnostics?token=$TOK"
```

Exit 0 = parity OK (modulo timestamps). Exit 1 = drift + structural diff.
Required clean-pass for §17 Stage 5/6 cutover.

## Cutover runbook (§17 Stage 6)

1. Deploy v1.4 alongside v1.3 (different port).
2. Run parity harness for 48 hours paper-mode (`--legacy` v1.3, `--new` v1.4).
3. After 48h clean: switch Coolify start command from `uvicorn app.main:app`
   to `uvicorn web.app:app`. Same DB, same env vars.
4. Keep v1.3 deployable for 7 days as fallback (do not delete `app/`).
5. After 7 days clean: archive `app/`.

## Safety / never-list

`docs/SYSTEM.md` §11 + `CLAUDE.md` are binding:

- Never push directly to `main` (use PRs via MCP).
- Never `--no-verify`, force-push, or destructive shell.
- Never touch venue credentials in code or DB.
- Every behavior-changing PR updates `docs/SYSTEM.md` in the same commit.
