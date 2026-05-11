# AutoTrader_Codex — System Manual

The single living document that describes **what this bot does, how it works, and how to debug it**. Every PR that changes behavior must update the relevant section. The diagnostics-monitor chat reads this file on every wake-up before judging anomalies.

> Status: **v0.1** — drafted 2026-05-11. Sections marked `[NEEDS OPERATOR INPUT]` need the user (Milind) to fill in venue/host-specific details that the code doesn't capture. Everything else is sourced directly from the codebase and recent session history.

---

## 0. What we are trying to accomplish

**Generate returns from market-neutral funding-rate arbitrage** on centralized perpetual-swap venues, with execution and risk management automated end-to-end. The bot is delta-neutral by construction (long spot + short perp on the same base asset), so its P&L comes from:

1. **Funding rate income** — the dominant return source on "hot" pairs (50% to 100,000%+ APY when annualized).
2. **Signed basis P&L** — entry-time basis kicker or cost, minus a worst-case adverse-exit assumption.
3. (Future) — cross-venue spread capture, onchain-perp / CEX-perp arb, IBKR equity / future hedges.

Operator wears a hands-off hat: deposit capital, set risk thresholds via `/config`, watch `/dashboard` and the diagnostics tracker.

---

## 1. Setup

### 1.1 Vultr (host)

| Setting | Value |
|---|---|
| Label | `arb-bot-tokyo` |
| Region | Tokyo |
| OS | Ubuntu 22.04 x64 |
| vCPU | 1 |
| RAM | 2048 MB (2 GB) |
| Storage | 64 GB NVMe |
| Public IPv4 | `45.32.53.166` (reverse DNS `45.32.53.166.vultrusercontent.com`) |
| Public IPv6 | `2001:8f8:1165:1275:5cd8:e3ba:9660:e28e` |
| Subnet mask | `255.255.252.0` |
| Default gateway | `45.32.52.1` |
| SSH username | `linuxuser` |
| Auto Backups | **NOT ENABLED** ⚠ |

- **SSH access**: `ssh linuxuser@45.32.53.166` (password is in the Vultr UI; rotate to key-based auth as a hardening step).
- Both Vultr IPs are whitelisted on the KuCoin API key's IP restriction list, so re-imaging the host or moving to a new instance will require re-whitelisting on KuCoin (and likely Binance too) before the bot can call them.
- OS: Linux (CVE-2026-31431 "Copy Fail" patched / `algif_aead` module disabled — see session memory 2026-05-10).
- **Risk: Auto Backups are not enabled.** The bot's SQLite DB (`bot.db`) lives on the local NVMe. If the instance dies, every trade record, naked_spot reconstruction, capital flow row, and equity-curve point is gone. Either enable Vultr Auto Backups for ~$1/mo, or run a cron that `scp`s the DB to a second host. Worth fixing before the bot accumulates a long P&L history.
- **Capacity note**: 1 vCPU was running at 41% under the pre-PR-20 paper-loop crash (every 30s loop iteration crashed with a NameError and burned CPU on exception handling + DB write). After PR #20 + #21 the load should drop substantially. If it stays above 50% sustained, consider bumping to 2 vCPU before adding more strategies.

### 1.2 Coolify (container deployment)

- Bot is deployed as a Coolify-managed service exposed at the public URL set by the sslip.io wildcard DNS (`http://m1348vwvjs47x081vz06b141.45.32.53.166.sslip.io`).
- Coolify auto-deploys on push to `main` via a GitHub webhook. ~1-2 minutes from `merge → live`.
- Build pipeline: **Nixpacks**. The `NIXPACKS_NODE_VERSION` env var (see below) is set even though the bot is Python-only; this is a Coolify default that lets Nixpacks build any optional frontend assets. Currently a no-op because the repo only contains `app/static/tables.js` (vanilla JS, no Node build step).
- Service-level env vars (set in Coolify UI, not in repo) — current state confirmed by operator 2026-05-11:
  - `BINANCE_API_KEY`, `BINANCE_API_SECRET`
  - `KUCOIN_API_KEY`, `KUCOIN_API_SECRET`, `KUCOIN_API_PASSPHRASE`
  - `DASHBOARD_USER`, `DASHBOARD_PASSWORD`
  - `DATABASE_URL`
  - `DIAGNOSTICS_TOKEN`
  - `NIXPACKS_NODE_VERSION` (build-time only; not read by app code)
- When you rename the GitHub repo, **re-confirm the Coolify webhook** in repo Settings → Webhooks. We've hit this before.

### 1.3 GitHub (source + CI + monitoring tracker)

- Repo: **`mbansia/AutoTrader_Codex`** (only repo accessible via the MCP allowlist).
- Branch protection on `main`: **none today** (operator-confirmed 2026-05-11). Merges to `main` happen via PR purely as a workflow convention (direct `git push origin main` is also blocked by an HTTP proxy, so PR-via-MCP is the only path that works). If you ever add protections (e.g. require status checks), update §11 of this doc — the monitor chat may need to wait on CI before merging.
- Dev branch convention: `claude/<short-kebab-description>`. We typically reuse a single long-lived dev branch (`claude/understand-repo-IeAcM`) and merge into `main` via PR.
- Required repo secrets (Actions → Secrets and variables → Repository secrets):
  - `BOT_URL` — bot's public URL (no trailing slash)
  - `DIAGNOSTICS_TOKEN` — same value as the bot's env var
- The cron workflow `.github/workflows/diagnostics.yml` runs every 3h.

### 1.4 Monitoring (diagnostics flow)

- `/api/diagnostics?token=...` on the bot returns a structured JSON snapshot (cycle health, positions, wallets, rejections grouped, recent events, anomalies).
- The cron workflow calls this endpoint and runs `.github/scripts/diagnostics_post.py` against the response.
- The script posts a comment to a single persistent tracker issue titled **`[bot-diagnostics] Tracker`** on every run, regardless of whether anomalies are present (heartbeat model). The issue body is updated to the latest full state. The comment is the webhook-firing event that wakes the monitor chat.
- A dedicated Claude monitoring chat subscribes to the tracker via `subscribe_pr_activity`, reads `docs/SYSTEM.md` (this file) on first wake, and responds inline. Response policy is in §11 — TL;DR briefly acknowledge "all clear" comments, react fully when anomalies are present.

### 1.5 Binance

- **Account**: `autotradercodex_virtual@yh0d2v3tnoemail.com` sub-account (per the operator's API Management screen).
- **Account type**: **Portfolio Margin (PM)** — visible in the sub-account API list as the "Portfolio ..." label and confirmed live in production via `/papi/v1/account` probes. All order routing goes through `/papi/v1/*`; Classic `/api/v3/*` and `/fapi/v1/*` calls return -2015 on a PM account.
- **API key**: `CTCDgU***` (HMAC type). Permissions granted (operator-confirmed 2026-05-11):
  - Spot trading
  - Margin trading
  - Futures trading
  - IP restriction enabled (only `45.32.53.166` whitelisted on Binance's side — confirm in the API key edit screen if you re-image the host)
- The bot's `BinanceGateway.is_unified_margin()` returns `True` unconditionally — this codebase treats every Binance account as PM. If you switch off PM, the bot will misbehave.
- **Universal transfer permission** isn't a separate Binance toggle on the screen the operator showed — it's implicit when Spot + Futures trading are both enabled on a PM account. If a transfer call ever returns -2014 / -1022, recheck the key.

### 1.6 KuCoin

- **Sub-account name**: `AutoTraderv2` (per the operator's API edit screen).
- **API key**: `69f88ba0b70d0a0001cf9523`.
- **Account mode**: **Classic** today (confirmed via `account_type()` probe at startup). The "Unified Account" toggle on the key permits UTA when the account is in UTA mode, but the master-account-side mode flip didn't take in this session; would require a master-account API call. Until UTA is enabled, the bot exercises the Classic-wallet code paths.
- Permissions granted (operator-confirmed 2026-05-11):
  - **General** — ticked and greyed-out (KuCoin baseline; cannot be disabled). Powers every read call (`fetch_balance`, `fetch_funding_rates`, `fetch_order_book`, …).
  - **Spot Trading** — required for spot leg order placement and dust conversion.
  - **Margin Trading** — required only if cross/isolated margin is used; harmless to leave on.
  - **Futures Trading** — required for the perp leg.
  - **Unified Account** — enables UTA mode when the account-side mode flip allows it. Today the sub-account is in Classic mode, so this permission is dormant.
  - **Allow Flexible Transfers** — REQUIRED for `consolidate_spot_wallets` and `transfer_*` calls (main↔trade↔contract↔margin↔isolated hops). If this is unticked, every wallet rebalance returns a permission error.
- **IP restriction**: `45.32.53.166` (IPv4) AND `2001:8f8:1165:1275:5cd8:e3ba:9660:e28e` (IPv6). Both Vultr IPs whitelisted. If the IP changes, KuCoin will silently 401 every call.
- KuCoin Classic has three+ spot wallets the bot interacts with:
  - `main` — Funding Account (default deposit destination)
  - `trade` — Trading Account (spot orders execute here)
  - `contract` — Futures wallet
  - `margin`, `isolated`, `pool` — also probed by `consolidate_spot_wallets` and `wallet_breakdown`. `pool` (KuCoin Earn) is excluded from sweeps because it's time-locked.

### 1.7 Onchain venues  `[NOT IMPLEMENTED — roadmap target still TBD]`

- Operator hasn't picked a chain/protocol yet. Candidates implied by `TRADE_TYPE_LABELS` in `app/models.py` include `<chain>_onchain_funding_arb` and `<venue>_cex_to_dex_funding_spread`.
- Will land as new `Gateway` subclasses in `app/exchange.py` (likely thin wrappers over a separate `app/onchain/<venue>.py` module that handles RPCs, wallets, and signing).

---

## 2. Variables / configuration

Three layers, in order of who edits them:

### 2.1 Environment variables (operator, set in Coolify)

Reference: `app/config.py` — `Settings` class.

| Var | Default | Used by |
|---|---|---|
| `BINANCE_API_KEY` / `_SECRET` | empty | Binance gateway init |
| `KUCOIN_API_KEY` / `_SECRET` / `_PASSPHRASE` | empty | KuCoin gateway init |
| `DASHBOARD_USER` | `admin` | HTTP Basic auth on `/dashboard`, `/config`, `/logs`, etc. |
| `DASHBOARD_PASSWORD` | `change-me` | same — operator must set a strong value |
| `DIAGNOSTICS_TOKEN` | empty | `/api/diagnostics?token=...` — endpoint returns 503 until set |
| `DATABASE_URL` | `sqlite:///./bot.db` | SQLAlchemy engine; mount on a persistent Coolify volume |
| `NIXPACKS_NODE_VERSION` | — | Coolify build pipeline (Nixpacks). Not read by Python app code; currently a no-op since the repo has no Node build step. |

### 2.2 StrategyConfig (operator-tuned, edited via `/config` UI, stored in DB)

Reference: `app/models.py` — `StrategyConfig` class.

| Field | Default | Meaning |
|---|---|---|
| `entry_funding_threshold` | 0.20 (=20% APY) | Minimum **net** annualized profit required to open a position. |
| `exit_funding_threshold` | 0.05 (=5% APY) | If a position's forward net APY drops below this, close it. |
| `max_hold_hours` | 72 | Hard time-based exit. |
| `max_open_positions` | 1 | Per mode, across venues. |
| `max_trades_per_day` | 8 | Soft cap on daily entries. |
| `min_position_pct` | 0.005 (0.5%) | Floor for sizing as % of equity. |
| `max_position_pct` | 0.10 (10%) | Ceiling for sizing as % of equity. |
| `enforce_hedge_check` | true | Verify both legs exist on the venue every cycle. |
| `delisting_check` | true | Force-close on market unhealthy. |
| `auto_transfer_enabled` | true | Pre-trade spot↔futures rebalance for Classic accounts. |
| `auto_quote_swap_enabled` | true | Auto-swap USDT↔USDC pre-trade when one quote is starved. |
| `auto_rebalance_threshold` | 1.0 | Imbalance bar (USDT) above which rebalance fires. |
| `futures_buffer_pct` | 0.20 | Margin buffer kept on the futures wallet during post-cycle drain. |
| `perp_leverage` | 1 | The only safe value for delta-neutral. |
| `max_perp_leverage` | 1 | Hard cap. |
| `max_entry_basis_bps` | (deprecated) | Field retained for back-compat; basis_dislocated gate was retired in favour of the profitability gate alone. |
| `max_exit_basis_bps` | 5.0 | Defer voluntary exits until basis is favourable. |
| `exit_basis_buffer_multiple` | 3.0 | Worst-case adverse-exit basis assumption: `worst_exit = entry + buffer × |entry|`. |
| `entry_tick_buffer_bps` | 1.0 | Limit-price padding above worst-walked-price for entry IOC. |
| `exit_tick_buffer_bps` | 2.0 | Same on exit. |
| `min_order_book_depth_usdt` | (defanged) | Legacy depth gate — the live book walk is the real check now. |
| `depth_band_bps` | (defanged) | Same. |
| `paper_starting_equity` | 1000 | Paper-mode virtual capital. |
| `paper_slippage_bps` / `paper_fee_bps` | 5 / 4 | Paper-mode synthetic fill costs. |
| `taker_fee_bps` | 5.0 | Fallback when venue fee API returns nothing. |
| `config_schema_version` | 1 (after migration) | Persisted migration cursor for one-shot config-value transforms. |

### 2.3 Module-level constants

Reference: `app/config.py` — defaults written into a fresh `StrategyConfig` row on first run.

| Constant | Default |
|---|---|
| `ENTRY_FUNDING_APR` | 0.20 |
| `EXIT_FUNDING_APR` | 0.05 |
| `MAX_HOLD_HOURS` | 72 |
| `LOOP_SECONDS` | 30 |
| `STOP_LOSS_PCT` | -0.02 |
| `PAPER_SLIPPAGE_BPS` | 5 |
| `PAPER_FEE_BPS` | 4 |

---

## 3. Strategies

### 3.1 Same-venue funding arb (`binance_same_venue_funding_arb`, `kucoin_same_venue_funding_arb`) — **ACTIVE**

**The bet**: when a perp's funding rate is positive (longs pay shorts), the bot opens long-spot + short-perp on that base. Funding settlements (every 4h or 8h) pay the short leg from the long leg's pocket — but our long-spot leg is on the SPOT market, not the perp's long leg, so we collect the funding without paying the funding (we hedge against price moves with spot, not with the perp's long side).

**Profit drivers**:
- **Funding income** = `funding_rate × notional × periods_held`. Dominant. Annualized via `(1+r)^N` compounding.
- **Signed basis P&L at entry** = `(perp_sell_price − spot_buy_price)`. Positive = kicker (perp at premium), negative = cost.
- **Round-trip basis P&L over the trade** = `entry_basis − exit_basis`. Modeled conservatively: worst-case exit = `entry + buffer × |entry|` (adverse direction is "more positive basis"), so round-trip = `−buffer × |entry|` for both signs of entry.
- **Fees**: 2 × spot taker + 2 × perp taker (entry + exit on each leg). Plus 2 × spot taker + 5 bps USDC/USDT basis when an auto-swap is needed.

**Exit conditions**:
- Forward net APY drops below `exit_funding_threshold`.
- `max_hold_hours` reached.
- Stop-loss: unrealized PnL / notional < `STOP_LOSS_PCT`.
- Naked leg detected (hedge integrity check) → close the surviving leg.
- Market unhealthy (delisting).
- Maintenance mode flipped on.

### 3.2 Cross-venue funding arb — **PLANNED**

Trade type tag: `cross_venue_funding_arb` (reserved in `TRADE_TYPE_LABELS`). Long perp on cheap-funding venue + short perp on expensive-funding venue. Not active yet.

### 3.3 Onchain — **PLANNED**

Trade type tag: TBD. Likely `<chain>_onchain_funding_arb`. Not active yet.

---

## 4. Cycle flow — what happens every loop iteration

Reference: `app/bot.py:run_one_cycle` (function name verbose, end of the file). Loop period = `cfg.loop_seconds` (default 30s).

```
┌──────────────────────────────────────────────────────────────────────┐
│ For each mode in (paper, live):                                       │
│   For each gateway in (binance, kucoin):                              │
│     ── Phase A: Safety (live mode only) ──                            │
│     ─ Per open position:                                              │
│       · check_market_health      → force_close_both if unhealthy      │
│       · check_hedge              → close_naked_leg if asymmetric      │
│     ─ recover_phantom_spot       → hedge-or-sell naked spot, +        │
│                                    persist as Position(naked_spot),   │
│                                    then dust-sweep at the end         │
│                                                                       │
│     ── Phase B: Exits ──                                              │
│     ─ funding_rates_dict (venue-aware override)                       │
│     ─ For each open position:                                         │
│       · update last_funding_rate                                      │
│       · forward profitability gate (same math as entry)               │
│       · if APY < exit_threshold OR age > max_hold OR stop-loss        │
│         → close (deferred when basis unfavourable, unless mandatory)  │
│                                                                       │
│     ── Phase C: Entries ──                                            │
│     ─ scan_funding                                                    │
│       · fetch_funding_rates → per-quote candidates                    │
│       · Tier-1: approx_net_apy = (1 + (funding − approx_fees)/1e4)^N  │
│         compare vs entry_funding_threshold                            │
│       · spot-pair existence check (forced markets reload if missing)  │
│       · rank by funding APY, ties broken by depth                     │
│       · log top-3 candidates' raw rate + interval + APY               │
│     ─ wallet prep:                                                    │
│       · consolidate_spot_wallets (KuCoin Classic: main/margin/        │
│         isolated → trade)                                             │
│       · wallet snapshot log                                           │
│       · pre-trade rebalance (gated on candidates_passing > 0 AND      │
│         not unified margin — PR #33)                                  │
│     ─ For top-5 candidates:                                           │
│       · skip if base in held_bases (incl. naked_spot)                 │
│       · auto-swap USDT↔USDC if same-stable arb is starved             │
│         (charges swap_RT in gate)                                     │
│       · iterative walk loop (≤4 passes):                              │
│         ── parallel simulate_fill (spot buy + perp sell)              │
│         ── compute provisional limit prices                           │
│         ── clamp target_qty by min(filled, reservation per leg)       │
│         ── repeat if shrinkage forced; break when converged           │
│       · profitability gate (full Tier-3 math, signed basis,           │
│         exit buffer, real venue fees from cache, +swap if any)        │
│       · place spot limit-IOC at worst_price + tick_buffer             │
│         ── pre-snapshot base balance for partial-fill detection       │
│         ── on exception: re-read balance, synthesize partial fill if  │
│           any quantity actually filled, continue at smaller qty       │
│       · persist Position(status='open') + spot Trade row              │
│       · place perp limit-IOC short at spot_filled_qty                 │
│         ── on failure: rollback spot via close_spot_limit_ioc         │
│       · reconcile leg sizes (trim spot if perp filled less)           │
│       · BREAK (max 1 open per cycle)                                  │
│                                                                       │
│     ── Post-cycle ──                                                  │
│     ─ ingest_capital_flows (deposits/withdrawals/sub-transfers)       │
│     ─ prune old rejected_candidates                                   │
│     ─ futures→spot drain (keep margin buffer; skip on unified)        │
│     ─ balance snapshot → EquityCurve                                  │
│                                                                       │
│   Crash handling: outer try/except logs                              │
│   `Loop iteration error (<mode>): <repr>` at ERROR and continues.    │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 5. Math — the gates in detail

### 5.1 Funding APY annualization

`annualize_rate(period_rate, interval_hours)` returns `(1 + r)^N − 1` where `N = 24 × 365 / interval_hours`.

```
8h funding,   1.0 bp / window   → APY ≈ (1.0001)^1095 − 1 ≈ 11.6%
8h funding,  10.0 bp / window   → APY ≈ 197%
4h funding,  62.0 bp / window   → APY ≈ 88,000%
```

Implemented in `app/exchange.py:annualize_rate`.

### 5.2 Tier-1 pre-filter (cheap)

For every perp returned by `funding_rates_dict`:
```
approx_fee_bps        = _approx_round_trip_fee_bps(symbol)   # 4 legs × cached
                                                                  per-symbol fee
approx_net_window_bps = funding_window_bps − approx_fee_bps
approx_net_apy        = (1 + approx_net_window_bps/1e4)^(24*365/interval_h) − 1
```
If `approx_net_apy < entry_funding_threshold` → bucket as `below_threshold` and skip Tier-2.

### 5.3 Tier-2 book walk

`simulate_fill(symbol, target_qty, side, perp)`:
- Calls `fetch_order_book(symbol, limit=K)` where K∈{20,100} (KuCoin REST restriction snapped automatically).
- Walks the asks (`buy`) or bids (`sell`) up to `target_qty`.
- Returns `{ok, filled_qty, avg_price, worst_price, levels, notional, error}`.

The bot runs spot and perp walks in parallel via `ThreadPoolExecutor` (ccxt is thread-safe for reads).

### 5.4 Tier-3 profitability gate (the real gate)

After the walk converges:

```python
fill_basis_bps           = (perp_avg − spot_avg) / spot_avg × 10_000
funding_window_bps       = candidate.funding_rate × 10_000
exit_buffer              = cfg.exit_basis_buffer_multiple             # default 3.0
worst_adverse_swing_bps  = abs(fill_basis_bps) × exit_buffer
rt_basis_signed_bps      = − worst_adverse_swing_bps                  # cost for both signs

spot_fee_bps             = gateway.taker_fee_bps(spot_symbol, perp=False)
perp_fee_bps             = gateway.taker_fee_bps(perp_symbol, perp=True)
round_trip_fees_bps      = 2 × (spot_fee_bps + perp_fee_bps)
if auto_swap_was_needed:
    swap_fee_bps         = 2 × (spot_fee_bps + 5)                     # 5 = USDC/USDT basis
    round_trip_fees_bps += swap_fee_bps

net_per_window_bps       = funding_window_bps + rt_basis_signed_bps − round_trip_fees_bps
periods_per_year         = 24 × 365 / interval_h
net_apy                  = (1 + net_per_window_bps/10_000)^periods_per_year − 1

if net_apy < cfg.entry_funding_threshold:
    REJECT as insufficient_annualized_profit
else:
    PROCEED
```

### 5.5 Reservation clamp (inside the walk loop)

Pre-PR-#10 bug: `target_qty = sized_notional / mid_price`, but the venue reserves at `qty × limit_price` (worst + tick), so for thin books the reservation could overflow the wallet by 2-5% and KuCoin would return `200004 Balance insufficient!` mid-fill (with a real partial fill landing).

Fix (since 2026-05 PR #20):

```python
for attempt in range(4):
    walk spot + perp in parallel
    spot_lim = spot_worst × (1 + tick/1e4)
    perp_lim = perp_worst × (1 − tick/1e4)
    max_by_spot  = (spot_leg_free × 0.99) / spot_lim
    max_by_perp  = (perp_leg_free × 0.99 × leverage) / perp_lim
    new_target   = min(spot_filled, perp_filled, max_by_spot, max_by_perp, target_qty)
    if new_target >= target_qty − 1e-9:   break    # converged
    target_qty = new_target
```

This guarantees `target_qty × limit_price ≤ leg_free × 0.99` (1% safety margin for fee accrual / rounding).

### 5.6 Exit-side mirror

Same formula, with `live_basis_bps` instead of `fill_basis_bps`. Sign-aware (`rt_basis_signed = −worst_adverse_swing`). If forward net APY < `exit_funding_threshold` → close.

---

## 6. Wallet model per venue

### 6.1 Binance Portfolio Margin (active)
- Unified pool: one balance per asset, used for both spot and perp.
- Synthesised `futures.<asset>.free` mirrors `spot.<asset>.free`; `futures.<asset>.total = 0` by convention to avoid double-counting in equity sums.
- `is_unified_margin() → True`.
- `transfer_*_to_spot` and `_to_futures` are no-ops (return `'PM mode: unified margin'`).
- Balance fetch: `/papi/v1/balance` (free = `crossMarginFree + umWalletBalance + cmWalletBalance`).

### 6.2 KuCoin Classic (active)
- Three+ separate spot wallets: `main`, `trade`, `contract`, `margin`, `isolated`, `pool`.
- `is_unified_margin() → False` (returns `self._is_uta`).
- Synthesised `spot.<asset>.free = main + trade` (aggregated). The actual spot order matching only touches `trade` — so we run `consolidate_spot_wallets` at the top of every cycle to sweep `main`/`margin`/`isolated` → `trade`, making the abstraction honest.
- `futures.fetch_balance({'currency': cur})` must be called per-currency (default returns USDT only). Implemented since PR #15. `wallet_breakdown` was missing this — fixed in PR #22 so `/api/diagnostics` reports the real USDC contract balance instead of always 0.
- **Transfer routing — futures→spot is a TWO-HOP.** Step 1: `self.futures.transfer(asset, amt, 'CONTRACT', 'MAIN')` (legacy `/api/v1/transfer-out`) is the only path that actually drains futures collateral. The spot-side `/api/v3/accounts/universal-transfer` returns code `112002 "Balance insufficient"` against the futures wallet (PR #29). Step 2: `self._transfer('main', 'trade', amt)` (spot inner-transfer) hops the funds into the trading wallet — KuCoin's `transferOut` lands them in `main` regardless of the `recAccountType=TRADE` hint ccxt sends. Without the second hop, funds sit in `main` for a cycle until `consolidate_spot_wallets` sweeps them, and the bot's drain/pre-trade-rebalance pair shuffles them back into `contract` in the meantime. `transfer_spot_to_futures` (IN direction) keeps using universal-transfer; that path works. Fixed in PR #29 (routing) + PR #33 (two-hop + idle-cycle gate).
- `pool` (KuCoin Earn) is time-locked, never swept.

### 6.3 KuCoin UTA (not active for the operator's sub-account)
- Single unified pool via `/api/v3/uta/account/balance`.
- `is_unified_margin() → True` (read live via `is_uta_enabled()` at gateway init).
- Same no-op transfer semantics as Binance PM.

### 6.4 Cross-stable USDT ↔ USDC
- Per-quote sizing: `spot_leg_free = spot_free_by_q[sq]`, `perp_leg_free = fut_free_by_q[cq]`. For cross-stable arbs `sq != cq`.
- Auto-swap fires only for same-stable arbs (`sq == cq`) when the relevant pool is below `min_notional` and the other stable has surplus.
- Swap path: `swap_quote(from, to, target, paper_mode)` walks USDC/USDT spot book, places limit-IOC at worst + tick, with a ±50 bps de-peg guard.

---

## 7. Database

SQLite by default (`bot.db`). Schema is defined in `app/models.py`; lightweight additive migrations (`ALTER TABLE ADD COLUMN`) run at startup via `app/db.py:run_schema_migrations`.

### 7.1 Tables

| Table | Purpose | Key columns |
|---|---|---|
| `strategy_config` | Singleton, operator-tuned. | All cfg.* fields. `config_schema_version` for one-shot migrations. |
| `mode_state` | Per-mode (paper/live) toggles. | `entry_enabled`, `exit_enabled`, `maintenance_mode`. |
| `strategy_state` | Per-(mode, trade_type) toggles. | `entry_enabled`, `exit_all_pending`. |
| `positions` | Lifecycle: open → naked_spot → closed. | `status`, `mode`, `exchange`, `trade_type`, `spot_symbol`, `perp_symbol`, `quantity`, `spot_entry_price`, `perp_entry_price`, `last_funding_rate`, `funding_income_accrued`, `last_close_error`. |
| `trades` | Every fill. | `position_id`, `venue` (spot/futures), `side`, `quantity`, `price`, `fee`, `ts`. |
| `bot_events` | Logs (INFO / WARN / ERROR). | `level`, `exchange`, `mode`, `message`, `ts`, `requires_action`. |
| `rejected_candidates` | Scan-time rejections for the Logs tab. | `mode`, `exchange`, `symbol`, `reason`, `funding_rate`, `ts`. |
| `balance_snapshots` | Per-cycle wallet snapshot. | `mode`, `exchange`, asset balances, totals. |
| `equity_curve` | Per-cycle equity history. | `mode`, `exchange`, `equity_usdt`, `ts`. |
| `capital_flows` | Deposits / withdrawals / sub-transfers (for XIRR). | `mode`, `exchange`, `amount_usdt`, `kind`, `external_id`, `detected_by`, `ts`. |
| `scan_results` | Per-cycle scan summary (for the Logs tab). | `mode`, `exchange`, `action`, `candidates_total`, `passing_total`, etc. |

### 7.2 Position lifecycle

```
        (entry path)            (recovery path)
            │                         │
            ▼                         ▼
       ┌────────┐                ┌────────────┐
       │  open  │                │ naked_spot │   ← partial fill under
       └────────┘                └────────────┘     spot_buy_error,
            │                         │             persisted with
   exit/close│                  hedge │             synthetic ghost-
            ▼                         │             entry Trade
       ┌────────┐                     ▼
       │ closed │  ◄──── sell ─── (try to short
       └────────┘   or dust-conv      matching perp)
                                      │
                                      ▼
                                 (back to 'open')
```

`OPEN_STATUSES = ('open', 'naked_spot')` is used by every "currently exposed" query so naked positions never disappear from the portfolio view.

**Rendering rule for `naked_spot`**: the perp leg of a `naked_spot` Position has `perp_entry_price = 0` because no perp short was ever filled (that's what makes it naked). The dashboard suppresses the perp-leg detail card for these rows and shows a "never opened (phantom spot)" tag instead, so no fabricated entry-price / MTM-PnL numbers appear. The spot-leg detail card is real.

**Stale reconciliation**: `recover_phantom_spot` runs a stale-naked-spot pass at the top of every live cycle. Any `naked_spot` Position whose underlying spot balance is gone from the wallet (sold externally, dust-converted by a prior cycle, venue Earn redemption, etc.) gets marked `closed` with `last_close_error = 'spot leg disappeared from wallet …'`. This stops the row from lingering in the open table indefinitely.

### 7.3 Migration policy

- Additive only — never drop columns. Code may stop reading a column; the column stays for back-compat.
- New columns get a sensible `DEFAULT` so existing rows remain valid.
- Idempotent (each `_add_column_if_missing` is a no-op when the column exists).
- One-shot value transforms (e.g. legacy per-period → APR threshold) are gated by `config_schema_version` so they run exactly once per row.

---

## 8. Monitoring & diagnostics

### 8.1 `/api/diagnostics?token=<DIAGNOSTICS_TOKEN>&hours=<1-168>`

Returns JSON (see `app/main.py:api_diagnostics`):

```
generated_at_utc, window_hours
cycle_health         { last_event_ts, last_event_msg, seconds_since_last_event, error_count, warn_count }
positions            { by_status, open: [...], naked: [...] }
wallets              { <venue>: { <asset>: { <wallet_type>: { free, total } } } }
rejections_grouped   { "<venue>/<mode>": { reason_category: count } }
rejections_total     int
recent_events        [{ ts, level, exchange, mode, msg }, ...]   # last 50 WARN/ERROR
recent_trades        [{ ts, mode, exchange, symbol, venue_leg, side, qty, price, fee }, ...]
recent_trades_count  int
anomalies            [{ severity, rule, detail }, ...]
anomalies_count      int
```

Auth: `?token=` is mandatory. 503 returned when `DIAGNOSTICS_TOKEN` env is unset (endpoint refuses to be silently public).

### 8.2 Anomaly rules (in `api_diagnostics` body)

| Rule | Severity | Trigger |
|---|---|---|
| `no_recent_events` | critical | no `BotEvent` in last 3600s |
| `stale_naked_spot` | warn | a `naked_spot` Position older than 60min |
| `no_trades_despite_scans` | warn | 0 recent trades AND >20 rejections in window |
| `error_burst` | warn | >20 ERROR `BotEvent`s in window |
| `close_blocked` | warn | open Position with non-empty `last_close_error` |

### 8.3 GitHub Actions cron

`.github/workflows/diagnostics.yml` runs every 3h. Required repo secrets:
- `BOT_URL` — public URL (no trailing slash)
- `DIAGNOSTICS_TOKEN` — matches bot env var

The cron pipes the JSON into `.github/scripts/diagnostics_post.py`, which (heartbeat model):
- Locates (or, on first run, creates) the persistent `[bot-diagnostics] Tracker` issue.
- Updates the issue body to the latest full state (anomalies + cycle health + positions + rejections + recent events + full JSON in `<details>`).
- Reopens the issue if someone manually closed it (the heartbeat issue stays open forever).
- Posts a one-comment-per-run heartbeat with terse status (✅ all clear or ⚠️ N anomalies + top-3). This comment fires the webhook to the monitor chat.
- Falls back to filing without a label if label creation fails.

### 8.4 Monitor chat

Separate Claude session from dev work. Reads `docs/SYSTEM.md` (this file) on every wake-up before judging. Subscribes to the tracker via `subscribe_pr_activity`. Responds inline on the tracker with diagnosis comments, opens PRs for code fixes via `mcp__github__create_pull_request`. Never pushes directly to `main`.

---

## 9. Logs & rejection categories — the diagnostic alphabet

### 9.1 Rejection categories (from `rejections_grouped`)

| Category | Source | Meaning | Action when dominant |
|---|---|---|---|
| `below_threshold` | Tier-1 pre-filter | Funding APY net of approx fees below entry threshold. | None — strategy designed to skip. |
| `no_spot_market` | scan | Perp's base has no spot pair on the venue (after force-reload). | None — perp-only listing. |
| `insufficient_annualized_profit` | Tier-3 gate | Real APY (with fill basis, real fees) below threshold. | None unless threshold mis-calibrated. |
| `below min position pct` | sizing | Wallet too small for `min_position_pct × equity`. | Inspect `wallets` for stranded funds; consider reducing `min_position_pct`. |
| `below_min_pct_after_clamp` | post-clamp re-check | Reservation clamp shrunk size below min. Genuinely too small. | None. |
| `no_book_depth` | book walk | `simulate_fill` returned 0; inner err embedded. | Inspect `spot_err`/`perp_err`; usually KuCoin limit issue (fixed) or symbol shape. |
| `reservation_clamp_zeroed` | clamp | Wallet too small even for limit-price reservation. | None. |
| `basis_dislocated` | (deprecated) | Gate was retired. **Should be 0**. | If non-zero, code regression — open PR. |
| `spot_buy_error: kucoin {Balance insufficient!}` | venue 200004 | Reservation overflow (pre-clamp era) or thin-book partial fill. | After PR #10/14/15 this should drop near zero. Watch for new occurrences. |
| `spot_buy_error: ... Order size below minimum` | venue 400100 | Sizing too small after clamp. | Investigate sizing math. |
| `spot_ioc_zero_fill` / `perp_ioc_zero_fill` | book moved during round-trip | Limit IOC didn't cross any level. | Transient; retries next cycle. |
| `strategy_disabled:<trade_type>` | strategy_state | Operator killed this strategy on `/config`. | None unless intentional. |

### 9.2 Common log patterns

- **`Spot wallet consolidate <asset>: X main→trade`** — KuCoin Classic sweep working. Diagnostic, ignore.
- **`Wallet snapshot <q> [Classic|UTA]·split|unified: spot free/total=...; fut free/total=...`** — per-cycle wallet state. Diagnostic.
- **`Pre-trade rebalance skipped: <venue> reports unified margin`** — PM/UTA correctly detected. Diagnostic.
- **`Pre-trade rebalance: X USDT spot→futures (equalize wallets so both legs can fund)`** — Classic-account rebalance working.
- **`Reservation clamp on <symbol>`** — *should not appear* (clamp moved inside walk loop). If it surfaces, regression.
- **`Scan top <symbol>: predicted rate=X% per Yh → APY=Z%`** — top-3 candidate diagnostic.
- **`Loop iteration error (<mode>): <traceback>`** — generic loop crash. Read the exception. Past examples: missing import (`total_funding_income`).
- **`Phantom spot RESCUED into a hedged position`** — hedge-or-sell working.
- **`Phantom spot CLOSED: sold ... → USDT`** — sell-back working.
- **`Dust sweep CLOSED N naked_spot position(s)`** — auto dust conversion working.
- **`futures→spot <quote> drain failed: kucoin Balance insufficient. 112002`** — known issue under investigation.

---

## 10. Failure modes & recovery

| Failure | Detection | Recovery |
|---|---|---|
| Partial fill under `spot_buy_error` | pre-/post-balance snapshot delta in entry path | Synthesize fill, continue to perp leg at smaller qty (PR #14) |
| Naked spot left behind | `recover_phantom_spot` scans every cycle | Hedge with matching perp if profitable, else sell back, else dust-convert (PR #15/21) |
| Dust below MIN_NOTIONAL | Notional check in recovery | `convert_dust_to_native` → BNB / KCS via venue dust endpoint (PR #21) |
| Wallet starvation | Sizing reject `below min position pct` | Wallet breakdown logged; operator inspects via `wallet_breakdown` |
| Book moves during round-trip | `spot_ioc_zero_fill` | Reject, retry next cycle |
| KuCoin futures→spot drain 112002 / 250001 / wallet oscillation | Post-cycle drain WARN repeating every cycle on idle accounts | **Fixed across PR #29 + PR #33.** Three layers: (1) routing — switched from spot-side universal-transfer to `self.futures.transfer('CONTRACT', 'MAIN')` (legacy `/api/v1/transfer-out`); (2) two-hop — append `main → trade` via spot inner-transfer so funds land where the spot order book can spend them; (3) idle-cycle gate — pre-trade rebalance no longer fires when `candidates_passing == 0`, breaking the drain↔rebalance oscillation that was producing 21k+ identical WARNs per 24h. Persistent failures are deduped via `_TRANSFER_ERROR_CACHE`. |
| Symbol drift across ccxt versions | Exit funding miss WARN | Falls back to stale `last_funding_rate`; logged so operator sees drift |
| Loop crash | Outer try/except in `run_one_cycle` | `Loop iteration error (<mode>): <repr>` logged at ERROR; loop continues next cycle |

---

## 11. Response policy (for the monitor chat)

Every cron run posts a heartbeat comment to the `[bot-diagnostics] Tracker` issue — anomalies or not — so the monitor chat ALWAYS gets a webhook. The response depends on what the comment says.

### Heartbeat "✅ all clear"

Reply with a **single concise message** confirming the check happened and summarising the state. One line is enough, e.g.

> ✅ Cron @ 2026-05-11T18:00Z — all clear. Positions `{open: 2, closed: 14}`, 8 trades in 24h, errors/warns `0/3`. No action.

This is the operator's proof the chain is alive. Don't pile on detail — the issue body has the full state already.

### Heartbeat "⚠️ N anomalies"

For each anomaly in the comment, decide and act per this table:

| Anomaly type | Action |
|---|---|
| Well-understood, no code change needed (e.g. dust will sweep next cycle) | **Comment** with one-line diagnosis. |
| Known transient (book moved, network blip) | **Skip** if anomaly clears next cycle; comment otherwise. |
| Clear code regression (NameError, broken endpoint, etc.) | **Open PR** with focused fix; mention the relevant section of this doc. |
| New venue error code not handled | **Open PR** adding handler + reject-reason taxonomy here. |
| Strategy / threshold change | **Ask** the operator on the tracker thread before acting. |
| Operator-action-required (e.g. no_usdt_pair on a Binance asset) | **Comment** with the manual step. |
| Anything ambiguous | **Ask** on the thread, don't act. |

Combine related anomalies in a single reply rather than one comment per anomaly. The operator wants a coherent diagnosis, not a wall of bullets.

**Never**: push to `main` directly, skip pre-commit hooks (`--no-verify`), force-push, run destructive shell commands, touch venue credentials.

PR workflow:
1. Branch: `claude/<short-kebab>`.
2. Small, focused. Don't refactor unrelated code.
3. Smoke: `python -c "import app.main"` + `curl /health` if the route surface changed.
4. Use `mcp__github__create_pull_request` + `mcp__github__merge_pull_request` (proxy blocks direct push).
5. **Update the relevant section of this doc** in the same PR.

---

## 12. Crons & scheduled jobs

| Job | Schedule | Trigger | Side effects |
|---|---|---|---|
| Bot's own loop | every `cfg.loop_seconds` (default 30s) | in-process thread per (mode, venue) | runs the full cycle in §4 |
| Diagnostics workflow | every 3h (`0 */3 * * *`) | GitHub Actions cron | hits `/api/diagnostics`, updates the persistent `[bot-diagnostics] Tracker` issue body, posts a heartbeat comment EVERY run (✅ all clear or ⚠️ N anomalies). Comment fires the webhook to the monitor chat. |
| (Future) onchain settlement watcher | TBD | TBD | TBD |

No external crons beyond these today. The bot is self-driving; the diagnostics cron exists only for human + monitor-chat oversight.

---

## 13. Glossary

- **APR**: simple-interest annualization (`r × N`). NOT what we use.
- **APY**: compounded annualization (`(1+r)^N − 1`). All thresholds are APY.
- **Basis** (perp − spot): perp price minus spot price, normalized; positive = perp at premium.
- **Funding rate**: periodic payment between perp longs and shorts. Sign convention: positive = longs pay shorts.
- **Funding window**: the interval at which funding is settled (4h or 8h on these venues).
- **Limit-IOC**: limit order with Immediate-Or-Cancel TIF. Either fills against existing depth right now or cancels (no resting order on the book). Taker fees.
- **Maker / taker**: maker rests on the book and gets matched; taker crosses the book and pays a higher fee.
- **MIN_NOTIONAL**: Binance's per-symbol minimum order notional in USDT. Below it, orders are rejected with -1013.
- **PM**: Binance Portfolio Margin — unified margin pool across cross-margin, USDM-futures, CM-futures.
- **UTA**: KuCoin Unified Trading Account — single unified pool across spot + futures.
- **Classic** (KuCoin): the non-UTA mode with separate `main`, `trade`, `contract`, `margin`, `isolated`, `pool` wallets.
- **Phantom spot**: a spot holding that exists on the venue but has no corresponding DB `Position` row — created by partial fills under spot_buy_error before recovery was wired.
- **Naked**: an unhedged leg (long-only spot or short-only perp). Bad state; recovery flattens or hedges.
- **Reservation**: the amount the venue's matching engine sets aside at order placement; for a limit buy it's `qty × limit_price`.

---

## 14. Doc-update policy (this is binding)

Every PR that changes BEHAVIOR — not just refactors — must update `docs/SYSTEM.md` in the same PR. Specifically:

- New strategy or trade-type → §3.
- New phase / step in the cycle → §4.
- Math change (gate, formula, threshold default) → §5.
- New venue / wallet type / transfer route → §6.
- New DB column or status value → §7.2.
- New `/api/*` endpoint or anomaly rule → §8.
- New rejection category or log pattern → §9.
- New failure mode + recovery path → §10.
- New env var → §2.1.
- New StrategyConfig field → §2.2.

Reviewers (human or monitor chat) reject PRs that change behavior without updating this doc. If unsure, add a one-line entry — better to over-document than under.

---

## 15. Known fragile / deferred work

- **Vultr Auto Backups are NOT enabled.** Single-instance SQLite DB on local NVMe. If `arb-bot-tokyo` dies, the entire trade / position / event history is lost. Either enable Vultr's automatic backup add-on, or run an off-host backup cron. Should be addressed before serious capital scales up.
- ~~**KuCoin `futures→spot` drain occasionally fails with 112002** despite positive `free` balance reported by `fetch_balance`. Possibly margin/order locks the transfer endpoint sees that the balance endpoint doesn't. Not crash-inducing but noisy. Investigation pending.~~ Resolved in PR #29 (routing) + PR #33 (two-hop + idle-cycle gate). See §6.2 and §10.
- **Maker-on-exit fee optimization** would save ~30% of exit fees. Not implemented; needs careful timeout-fallback logic to avoid leaving resting orders that the basis can run away from.
- **Cross-venue funding arb** strategy is tagged in `TRADE_TYPE_LABELS` but the orchestrator isn't wired.
- **Onchain integration** is a future track. No code yet.
- **Symbol mapping drift** across ccxt versions could leave open positions un-lookupable for exit funding refresh. Currently logs a WARN and falls back to stale `last_funding_rate`.

---

## 16. Recent changes log (auto-extended on every behavior-changing PR)

| Date (UTC) | PR | Section(s) touched | Summary |
|---|---|---|---|
| 2026-05-11 | #21 | §10, §11 | Auto-convert dust to BNB/KCS via venue endpoints. |
| 2026-05-11 | #20 | §9, §10, §11 | Fix `total_funding_income` NameError. Silence dust spam. Filter LDUSDT. Workflow label fallback. |
| 2026-05-11 | #18 | §8, §11, §12 | `/api/diagnostics` endpoint + GitHub Actions cron + tracker. |
| 2026-05-11 | #17 | §4, §7.2, §9 | Naked positions are first-class in dashboard + transactions. `OPEN_STATUSES`. |
| 2026-05-11 | #16 | §5.4 | Charge auto-swap fees in profitability gate. |
| 2026-05-11 | #15 | §6.1, §10 | Hedge phantom spot via perp when profitable. KuCoin futures per-currency fetch. |
| 2026-05-11 | #14 | §4, §10 | Recover orphaned spot positions + partial-fill detection under spot_buy_error. |
| 2026-05-11 | #13 | §6.4, §10 | KuCoin sweep margin/isolated + `wallet_breakdown` diagnostic. |
| 2026-05-11 | #12 | §5, §6.1, §6.3, §7.2 | Audit cleanups: reservation clamp in walk loop, exit funding refresh, sign math, migration v1, dead-config purge. |
| 2026-05-11 | #11 | §9 | Fix `below_threshold` log to show net APY. |
| 2026-05-10 | #10 | §5.5 | Reservation-aware target_qty clamp. |
| 2026-05-10 | #9 | §5.4 | Dropped `basis_dislocated` gate; profitability-only economic check. |
| 2026-05-10 | #8 | §5.4, §5.5 | KuCoin book-walk limit fix, sign-aware basis, funding APY diagnostic. |
| 2026-05-10 | #7 | §6.4 | KuCoin Classic spot-wallet consolidation. |

(Older history in `git log` and the session memory.)

---

> **For the monitor chat:** Always read this doc from the latest `main` before judging anomalies. The rejection-category meanings (§9), failure-mode recovery (§10), and response policy (§11) are your operating manual.
