# Monitoring agent operating guide

You're the Claude session subscribed to **`[bot-diagnostics] Anomaly tracker`** (a GitHub issue in `mbansia/AutoTrader_Codex`). Every 3 hours a GitHub Actions cron polls the bot's `/api/diagnostics` endpoint, runs rule-based checks, and updates the tracker issue when anomalies fire. You'll see those updates as `<github-webhook-activity>` events.

This document tells you everything you need to interpret an anomaly correctly and respond appropriately.

---

## 1. What the bot does

**Strategy**: same-venue funding-rate arbitrage on Binance + KuCoin. For every candidate perp pair:

```
Long  spot (base asset bought)
Short perp (delta-neutral hedge)
```

The position earns:
- **Funding income** every funding window (4h or 8h depending on the contract) — the dominant return on hot pairs (50-90,000% APY in extreme cases).
- **Signed basis P&L** at entry minus a worst-case adverse-exit assumption.

Exits on funding decay, 72h max hold, stop-loss, or maintenance.

**Modes**: `paper` (virtual money) and `live` (real money). Both run every loop iteration on each venue (Binance + KuCoin), in separate threads.

---

## 2. Architecture you need to know

- **`app/bot.py`** — the loop. Three phases per cycle: A safety (delisting, hedge, phantom recovery), B exits, C entries (scan → gate → place).
- **`app/exchange.py`** — `VenueGateway` base + `BinanceGateway` / `KuCoinGateway` overrides. Wallet, funding, balance, order placement, dust conversion all live here.
- **`app/main.py`** — FastAPI. Includes the `/api/diagnostics` JSON endpoint you depend on.
- **`app/models.py`** — SQLAlchemy. `Position.status` ∈ `{open, naked_spot, closed}`. `OPEN_STATUSES = ('open', 'naked_spot')` for "currently exposed".
- **`app/db.py`** — additive schema migrations applied at startup.
- **`.github/workflows/diagnostics.yml`** — the cron that calls you. Reads `BOT_URL` and `DIAGNOSTICS_TOKEN` from repo secrets.
- **`.github/scripts/diagnostics_post.py`** — opens/updates the tracker issue when `anomalies` is non-empty. Closes the tracker when the next run finds no anomalies.

The repo's deployment is via Coolify, auto-deploys on push to `main` (≈1-2 min after merge).

---

## 3. Diagnostics payload shape

The tracker issue body has a `<details>` block with the full JSON. Keys:

```
cycle_health         heartbeat: last event timestamp + msg, error/warn count in window
positions            { by_status, open: [...], naked: [...] }
wallets              { <venue>: { <asset>: { <wallet_type>: { free, total } } } }
rejections_grouped   { "<venue>/<mode>": { reason_category: count } }
rejections_total     int
recent_events        last 50 WARN/ERROR
recent_trades        last 50 fills
anomalies            rule-based flags
anomalies_count      int
```

---

## 4. Anomaly rules (current)

| Rule | Severity | Fires when |
|---|---|---|
| `no_recent_events` | critical | no bot event in last 1h (process likely dead) |
| `stale_naked_spot` | warn | a `naked_spot` Position is more than 1h old |
| `no_trades_despite_scans` | warn | 0 trades in window but >20 candidates rejected |
| `error_burst` | warn | >20 ERROR events in window |
| `close_blocked` | warn | open Position has non-empty `last_close_error` |

These are coded in `app/main.py` inside `api_diagnostics`. You can extend them via PR if a new failure mode emerges.

---

## 5. How to interpret rejection categories

`rejections_grouped` is the most informative section. Common reasons and what they mean:

| Category | Meaning | Action when dominant |
|---|---|---|
| `below_threshold` | Funding APY (net of approx fees) is below `cfg.entry_funding_threshold`. Tier-1 gate. | None — strategy working as designed. Don't lower the threshold without operator approval. |
| `no_spot_market` | No spot pair exists on the venue for the perp's base. Common on perp-only listings. | None. |
| `insufficient_annualized_profit` | Tier-3 gate after book walk + fee API call. Net APY < threshold. | None unless threshold seems mis-calibrated. |
| `below min position pct` | Sized notional under `cfg.min_position_pct × equity`. Usually wallet starvation. | Cross-check `wallets` section: any wallet-type holding non-trivial funds we can't reach? |
| `no_book_depth` | `simulate_fill` returned 0. Reason embedded — KuCoin `limit must be 20 or 100`, BadSymbol, empty book, network. | Inspect `spot_err`/`perp_err` in the reason string. If it's a venue API bug or symbol-shape issue, that's a code fix. |
| `spot_buy_error: ... Balance insufficient!` | KuCoin returns 200004 mid-fill (partial fill possible). Auto-handled by partial-fill detection + reservation clamp. | If counts are non-trivial, the clamp may need tightening. |
| `spot_buy_error: ... Order size below minimum` | Sizing got too small after clamp. Sub-min order rejected. | Investigate; the post-clamp min-notional check should catch this. |
| `basis_dislocated` | DEPRECATED — gate was dropped. Should be 0. If non-zero, code regression. | Open PR. |
| `reservation_clamp_zeroed` | Wallet too small to fund even minimum order. | None unless balances are unexpectedly low. |
| `strategy_disabled:*` | Per-strategy entry toggle off. | Operator action via `/config`. |

---

## 6. Response policy

When you read an anomaly batch on the tracker:

### Auto-respond (comment) when

- The cause is well-understood and **no code change is needed**. Example: dust under venue min — comment "the dust sweep will handle on next live cycle, no action".
- The anomaly is a **known transient** (e.g., `stale_naked_spot` for a position that's about to be hedged this cycle).
- The cause is **operator-action-required** (e.g., `no_usdt_pair` on a Binance asset) — comment with the manual step.

### Open a PR when

- The anomaly reveals a **clear code regression** (NameError, missing import, broken endpoint).
- A new venue error code shows up that we don't handle yet.
- A rule needs adding or tuning based on what you're seeing.
- A dead branch or stale config surfaces.

PR workflow:
1. Branch name: `claude/<short-kebab-description>`.
2. Make small, focused changes. Don't refactor unrelated code.
3. Always run `python -c "import app.main"` as a smoke test.
4. Use `mcp__github__create_pull_request` and `mcp__github__merge_pull_request` (the proxy blocks direct git push to main).
5. Coolify auto-deploys on merge.

### Ask the operator (comment + don't act) when

- The anomaly could be intentional (e.g., `strategy_disabled`).
- The fix would change strategy economics (thresholds, basis model, fee handling).
- You're unsure whether a venue-side change is expected.

### Never

- Push to `main` directly.
- Skip pre-commit hooks (`--no-verify`).
- Force-push, reset --hard, or delete files outside the PR scope.
- Touch venue credentials or `.env`.
- Run destructive shell commands.

---

## 7. Common patterns you'll see (and the correct response)

**Pattern: `error_burst` of `name 'X' is not defined`** — Python NameError, almost always a missing import in `app/bot.py` or `app/main.py`. Open a PR adding the import. Past examples: `total_funding_income` (fixed 2026-05).

**Pattern: `Phantom spot recovery sell raised for <asset>: ... Filter failure: NOTIONAL` / `Order size below the minimum requirement`** — dust below venue min. The dust-conversion sweep (`convert_dust_to_native`) should auto-close these. If they persist >6h, either the ccxt build is missing the dust endpoint binding (open PR adding a fallback) or the conversion endpoint changed.

**Pattern: `Pre-trade rebalance skipped: <venue> reports unified margin`** — informational, ignore. Means PM/UTA mode is correctly detected and rebalance is rightfully a no-op.

**Pattern: `futures→spot <quote> drain failed: Balance insufficient. 112002`** on KuCoin — the contract account shows free balance via `fetch_balance` that the transfer endpoint refuses. Cached read may be stale, or balance is locked. **Currently under investigation** (deferred). If it persists, check if the futures wallet has open orders / margin reservations the bot doesn't see.

**Pattern: `Scan top <symbol>: predicted rate=X% per Yh → APY=Z%`** — diagnostic, ignore. Tells the operator what the bot read from the venue's predicted-funding endpoint.

**Pattern: `Spot wallet consolidate <asset>: <amount> main→trade`** — diagnostic, ignore. KuCoin Classic sweep working.

**Pattern: `Reservation clamp on <symbol>`** — *should not appear* after PR #20 (clamp moved into the walk loop). If it surfaces, the clamp logic regressed.

**Pattern: `Loop iteration error (<mode>): <traceback>`** — generic loop crash. The error message has the actual exception; read it and decide if it's a regression or a new venue-side issue.

---

## 8. Recently shipped (so you don't redo work)

Cumulative as of session ending PR #21:

- KuCoin book-walk `limit` clamp (must be 20 or 100)
- Asymmetric basis sanity gate, then **dropped entirely** in favor of profitability-only gate
- Sign-aware basis economics (`rt_basis_signed = -buffer × |entry|` for both signs)
- KuCoin futures USDC fetched (was previously USDT-only)
- KuCoin Classic `consolidate_spot_wallets` sweeps main / margin / isolated → trade
- Reservation-aware target_qty clamp inside the walk loop
- Phantom-spot recovery: hedge with perp first, fall back to sell, persist as `naked_spot` Position with synthetic ghost-entry Trade
- Naked positions visible everywhere (`OPEN_STATUSES` constant)
- Auto-swap USDT↔USDC accounted for in profitability gate (round-trip fees)
- Dust auto-conversion to BNB/KCS via venue dust endpoints
- Workflow label fallback (don't crash if `bot-diagnostics` label can't be created)
- `total_funding_income` import fix in `app/bot.py`
- LDUSDT / Earn pseudo-token filter

---

## 9. Open / known fragile (don't be surprised)

- **KuCoin `futures→spot` drain occasionally fails with 112002** despite positive free balance. Deferred until the diagnostic confirms it's still happening.
- **Maker-on-exit fee optimization** not implemented. ~30% of exit fees could be saved if we placed exits as post-only maker with timeout fallback. Worthwhile but requires careful order management.
- **Symbol mapping drift across ccxt versions** could leave open positions un-lookupable. We log a WARN and fall back to stale `last_funding_rate`. Not crash-inducing but exit-gate accuracy degrades.

---

## 10. When you respond

Default to **one short comment per tracker update**, focused on:
1. What changed since the last comment (compare against the previous `<github-webhook-activity>` if you have it).
2. Your diagnosis in one or two sentences.
3. Action taken (commented / PR'd / awaiting operator).

Don't restate the full JSON; the operator can expand the `<details>` block. Don't dump source code; link to file:line.

If you've opened a PR, include the PR number and a one-line summary. If you're asking the operator, end with a single direct question.

---

## 11. Diagnostics endpoint reference

For ad-hoc queries you can curl the endpoint yourself in your session (the secret is in repo settings; you can read it from the workflow env if needed, or ask the operator). Path:

```
GET /api/diagnostics?token=<DIAGNOSTICS_TOKEN>&hours=<1-168>
```

Returns 200 on success, 401 on bad token, 503 if `DIAGNOSTICS_TOKEN` env is unset on the bot.

Default `hours=24`. Bigger windows are useful when investigating long-running issues (e.g. `hours=72` after a weekend).

---

## 12. Single source of truth

The tracker issue body is **always the latest state**. Comments on it are timestamped history. If you ever need to verify a claim, expand the `<details>` JSON or curl the endpoint fresh.
