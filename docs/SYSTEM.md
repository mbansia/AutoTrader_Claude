# AutoTrader_Codex — System SSOT

The single living source of truth for what this bot does and how it works. Every behavior-changing PR updates the relevant section in the same commit. The diagnostics-monitor chat re-reads this on every wake-up before judging anomalies; any new dev reads it once to onboard.

> Status: **v1.0** — 2026-05-11 rewrite after operator audit.

---

## Table of contents

- [0. Definitions](#0-definitions)
- [1. Purpose](#1-purpose)
- [2. Setup](#2-setup)
- [3. Strategies](#3-strategies)
  - [3.1 Same-venue funding arbitrage (active)](#31-same-venue-funding-arbitrage-active)
  - [3.2 Cross-venue funding arb (planned)](#32-cross-venue-funding-arb-planned)
  - [3.3 Onchain (planned)](#33-onchain-planned)
- [4. Configuration](#4-configuration)
- [5. Wallet model per venue](#5-wallet-model-per-venue)
- [6. Database schema](#6-database-schema)
- [7. Monitoring & diagnostics](#7-monitoring--diagnostics)
- [8. Logs & rejection categories](#8-logs--rejection-categories)
- [9. Failure modes & recovery](#9-failure-modes--recovery)
- [10. Response policy (monitor chat)](#10-response-policy-monitor-chat)
- [11. Crons](#11-crons)
- [12. Doc-update policy](#12-doc-update-policy)
- [13. Known fragile / deferred](#13-known-fragile--deferred)
- [14. Changelog](#14-changelog)

---

## 0. Definitions

These terms appear throughout. Read them first.

| Term | Definition |
|---|---|
| **Funding rate** | Periodic payment between perp longs and shorts. *Positive* funding = longs pay shorts. Settled at the **funding window**. |
| **Funding window** | The interval at which funding is settled. Typically 4h or 8h, contract-specific. Read per-pair from the venue. |
| **APR** | Annualized rate using simple (non-compounded) addition: `r × N` where N = periods/year. **Not used in this codebase.** |
| **APY** | Annualized rate using compounding: `(1 + r)^N − 1`. **All thresholds in this codebase are APY**, even when historical variable names say "APR". |
| **Spot** | Cash market — `BASE/QUOTE` pair, e.g. `BTC/USDT`. Buying spot = owning the base asset. |
| **Perp** | Perpetual futures contract — `BASE/QUOTE:QUOTE`, e.g. `BTC/USDT:USDT`. No expiry; held positions accrue/pay funding. |
| **Basis** | `(perp_price − spot_price) / spot_price`, in bps. **Positive** = perp at a premium to spot. |
| **Long spot + short perp** | This bot's only active structure. Net price exposure = 0 (delta-neutral). Earns funding when funding rate is positive. |
| **Entry basis** | Basis at the moment we open: `(perp_sell_fill − spot_buy_fill) / spot_buy_fill × 10⁴`. Positive = we sold the perp at a premium = entry profit. |
| **Worst-case adverse exit basis** | Conservative assumption about the basis when we eventually close. Model: `entry_basis + buffer × |entry_basis|`, where buffer = `cfg.exit_basis_buffer_multiple` (default 3.0). |
| **Limit-IOC** | Limit order with Immediate-Or-Cancel time-in-force. Fills against existing depth at or better than the limit, cancels any remainder. We never leave resting orders. Pays taker fee. |
| **Reservation** | Cash a venue's matching engine sets aside when a limit-buy is placed: `qty × limit_price` from the trade wallet. If this exceeds the wallet's free balance, the venue rejects mid-fill (e.g. KuCoin `200004 Balance insufficient!`). |
| **Naked spot** | A spot holding with no matching perp short. Created when a partial fill under `spot_buy_error` left the perp leg unfilled. Stored as `Position(status='naked_spot')`. |
| **Phantom spot** | Same as naked spot — older terminology. We persist them so the dashboard never shows a fabricated number. |
| **PM** | Binance **Portfolio Margin** — unified margin pool across cross-margin / USDM-futures / CM-futures. Orders route through `/papi/v1/*`. |
| **UTA** | KuCoin **Unified Trading Account** — single unified pool across spot + futures. |
| **Classic** (KuCoin) | The non-UTA mode. Spot funds split across `main`, `trade`, `margin`, `isolated`, `pool`; futures in `contract`. |
| **MIN_NOTIONAL** | Venue's per-symbol minimum order notional. Binance returns `-1013 NOTIONAL` below it; KuCoin returns `400100 minimum requirement`. Roughly $5 on Binance, $1 on KuCoin. |
| **Net APY** | Annualized profit AFTER all costs (worst-case basis, round-trip fees, swap fees if any). The gate threshold (`entry_funding_threshold` / `exit_funding_threshold`) is in this unit. **NOT the raw funding APY.** |
| **Round-trip** | Entry + exit on both legs = 4 fee-bearing trades. If the bot needed a USDT↔USDC swap to fund the trade, +2 more swap legs. |
| **Heartbeat tracker** | The persistent GitHub issue (`[bot-diagnostics] Tracker`) the cron updates and comments on every 3h. |
| **Cross-stable arb** | A funding-arb candidate whose perp quote currency differs from the spot quote — e.g. spot leg on `DOGE/USDT`, perp leg on `DOGE/USDC:USDC`. Per-quote sizing reads the spot leg from the spot-quote wallet and the perp leg from the perp-quote wallet; these are independent. |
| **Same-stable arb** | Spot and perp use the same quote (both USDT or both USDC). If the wallet for that quote is empty but the other stable has surplus, the `auto-swap` path can fund the trade. |
| **Tier-1 / Tier-2 / Tier-3** | The three gates a candidate passes before the bot will trade it. **Tier-1**: cheap pre-filter on annualized net APY using approximate fees and zero basis (in `scan_funding`). **Tier-2**: book-walk via `simulate_fill` confirming both legs can fill at the sized qty (gives real avg/worst prices). **Tier-3**: full profitability gate using actual fill prices, live per-symbol fees, signed basis, swap-fee surcharge if needed. Only candidates that pass all three reach order placement. |
| **Paper vs live mode** | Two parallel execution paths. **Paper** uses synthetic fills (`paper_slippage_bps`, `paper_fee_bps`) against real venue prices — no orders sent, no real money at risk. **Live** sends real orders. Both run every cycle on every gateway. Their DB rows are kept separate via the `mode` column. |
| **Mandatory vs voluntary exit** | **Voluntary** exits (`forward_profit_below_threshold`, `max_hold`) are deferred when live basis is unfavourable — closing right now would print an extra basis cost. **Mandatory** exits (`stop_loss`, `check_hedge` naked-leg, `check_market_health` delisting) close immediately regardless of basis. |

---

## 1. Purpose

Generate returns from **market-neutral funding-rate arbitrage** on centralized perpetual-swap venues. Operator deposits capital, sets risk thresholds via `/config`, watches `/dashboard` and the diagnostics tracker. The bot decides what to open, when to open, when to exit, and how to recover from failure modes — without human intervention in the normal case.

**Active strategies:** 1 (same-venue funding arb on Binance + KuCoin). **Planned:** cross-venue arb, onchain.

---

## 2. Setup

### 2.1 Vultr (host)

| Setting | Value |
|---|---|
| Label | `arb-bot-tokyo` |
| Region | Tokyo |
| OS | Ubuntu 22.04 x64 |
| vCPU / RAM / disk | 1 vCPU · 2 GB · 64 GB NVMe |
| Public IPv4 | `45.32.53.166` (reverse DNS `45.32.53.166.vultrusercontent.com`) |
| Public IPv6 | `2001:8f8:1165:1275:5cd8:e3ba:9660:e28e` |
| SSH | `ssh linuxuser@45.32.53.166` (password in Vultr UI) |
| Auto Backups | **NOT ENABLED** ⚠ (see §13) |

Both IPs are whitelisted on KuCoin's API key. Re-imaging or migrating instances requires re-whitelisting on KuCoin (and Binance if IP-restriction is enabled there).

OS-level CVE-2026-31431 ("Copy Fail") patched / `algif_aead` disabled — session memory 2026-05-10.

### 2.2 Coolify

Deploys via Nixpacks on push to `main`. Webhook is the only deploy trigger. ~1-2 min from merge → live container.

**Environment variables** (set in the Coolify service UI):

| Var | Purpose |
|---|---|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Binance API auth |
| `KUCOIN_API_KEY` / `KUCOIN_API_SECRET` / `KUCOIN_API_PASSPHRASE` | KuCoin API auth |
| `DASHBOARD_USER` / `DASHBOARD_PASSWORD` | HTTP Basic auth on every UI route except `/health` and `/api/diagnostics` |
| `DIAGNOSTICS_TOKEN` | Token for `/api/diagnostics?token=...`. Endpoint returns `503` if unset. |
| `DATABASE_URL` | SQLAlchemy connection (defaults to `sqlite:///./bot.db`) |
| `NIXPACKS_NODE_VERSION` | Coolify build pipeline; not read by application code. |

### 2.3 GitHub

- Repo: **`mbansia/AutoTrader_Codex`** (only repo accessible via the GitHub MCP allowlist).
- Branch protection on `main`: **none**. All merges via PR are workflow convention (a proxy blocks direct `git push origin main`).
- Dev branch: `claude/<short-kebab>` (reuse `claude/understand-repo-IeAcM`).
- Required repo secrets (Settings → Secrets and variables → Actions → Repository secrets):
  - `BOT_URL` — `http://m1348vwvjs47x081vz06b141.45.32.53.166.sslip.io` (no trailing slash)
  - `DIAGNOSTICS_TOKEN` — matches the bot's env var
- Workflow: `.github/workflows/diagnostics.yml`, cron `0 */3 * * *`.

### 2.4 Binance

- **Account**: `autotradercodex_virtual@…` sub-account.
- **Type**: **Portfolio Margin** (PM). Confirmed at runtime via `/papi/v1/account`. Bot routes through `/papi/v1/*` — Classic endpoints return `-2015` on this account.
- **API key**: `CTCDgU***` (HMAC), IP-restricted to `45.32.53.166`.
- **Permissions enabled**: Spot trading · Margin trading · Futures trading.
- The bot's `BinanceGateway.is_unified_margin()` returns `True` unconditionally — this codebase assumes PM. Switching the account off PM mode will break sizing / transfer paths.

### 2.5 KuCoin

- **Sub-account**: `AutoTraderv2`.
- **Account mode**: **Classic** (confirmed via `account_type()` probe at startup). UTA-enabling didn't take from the sub-account UI; would need a master-account API call.
- **API key**: `69f88ba0b70d0a0001cf9523`. IP-restricted to both Vultr IPs (v4 + v6).
- **Permissions enabled**:
  - General (read-only baseline; locked-on, can't be disabled)
  - Spot Trading · Margin Trading · Futures Trading
  - Unified Account (dormant until account-mode flips to UTA)
  - Allow Flexible Transfers — **required** for `consolidate_spot_wallets` and all `transfer_*` calls

### 2.6 Onchain — `TBD`

Roadmap target. No code yet. Will land as new `Gateway` subclasses in `app/exchange.py`.

---

## 3. Strategies

This codebase is multi-strategy by design — each strategy has its own SOP and (eventually) its own config. **Today only same-venue funding arb is active**, so the shared `StrategyConfig` row is effectively the funding-arb config. When additional strategies land, expect this section to grow and the configuration model in §4 to split.

### 3.1 Same-venue funding arbitrage [ACTIVE]

**Trade-type tags:** `binance_same_venue_funding_arb`, `kucoin_same_venue_funding_arb`.

#### Thesis

When a perp pays positive funding (longs pay shorts), the bot opens a delta-neutral structure on that base asset:

```
LONG spot   (own the base asset)
SHORT perp  (hedge price + collect funding from longs)
```

Net price exposure ≈ 0. Returns come from:
1. **Funding income** every funding window (dominant on hot pairs).
2. **Entry basis kicker** if we sell the perp at a premium to where we buy the spot.
3. (Negative) **Worst-case adverse basis swing** between entry and exit.
4. (Negative) **Round-trip taker fees** (2 spot legs + 2 perp legs + optionally 2 USDC↔USDT swap legs).

#### SOP per loop iteration

Loop period: `cfg.loop_seconds` (default 30s). Runs separately for paper and live mode, on each gateway (Binance, KuCoin).

```
Phase A — Safety (live only)
   For each open Position:
      check_market_health → force_close_both if delisted/halted
      check_hedge          → close_naked_leg if one leg disappeared
   recover_phantom_spot
      ① Stale reconciliation: any naked_spot Position whose spot wallet
         balance is gone → mark closed
      ② For each non-stable spot asset with notional ≥ $0.10:
         - if notional < venue MIN_NOTIONAL → persist as
           naked_spot(dust), skip recovery
         - else: persist as naked_spot, try hedge (Phase 1) or sell (Phase 2)
      ③ Dust sweep: batch convert_dust_to_native for all
         naked_spot(dust) positions → BNB / KCS via venue dust endpoint;
         on success, mark them closed

Phase B — Exits (when exit_enabled)
   gateway.funding_rates_dict() — fresh predicted rates per venue
   For each open Position:
      update p.last_funding_rate, p.funding_interval_hours
      compute forward-looking net APY at LIVE funding + basis (math below)
      exit_reason set if:
         forward_profit_below_threshold  (forward net APY < exit threshold)
         max_hold                        (age > cfg.max_hold_hours)
         stop_loss                       (unrealized PnL / notional ≤ cfg.stop_loss_pct) [mandatory]
      Non-mandatory exits deferred when basis is unfavourable
      Mandatory: force_close_both regardless

Phase C — Entries (when entry_enabled, not at max_open_positions)
   scan_funding (per gateway)
      fetch perp funding rates
      Tier-1 pre-filter (cheap, per-pair):
         approx_net_apy = annualize(funding - approx_fees)
         REJECT below_threshold if approx_net_apy < entry_funding_threshold
      spot-pair existence check (reload markets if missing)
      rank passing candidates by funding APY desc
   Wallet prep (live only):
      consolidate_spot_wallets (KuCoin Classic: main/margin/isolated → trade)
      pre-trade rebalance     (only when candidates_passing > 0 AND split-
                                wallet venues: equalize spot ↔ futures.
                                Gated on candidates so idle cycles don't
                                shuffle wallets, see §5.2 + PR #33)
      auto-swap USDT↔USDC      (if a same-stable arb is starved)
   For each top-5 candidate:
      skip if base already held (incl. naked_spot)
      iterative walk loop (≤4 passes):
         parallel simulate_fill on spot (buy) + perp (sell)
         compute provisional limit prices
         clamp target_qty by min(filled, spot reservation, perp reservation)
         break when converged
      Tier-3 profitability gate (math below)
         REJECT insufficient_annualized_profit if net_apy < entry_funding_threshold
      Place spot limit-IOC (pre-snapshot wallet for partial-fill detection)
         on exception with non-zero balance delta → synthesize partial fill,
         continue at smaller qty
      Persist Position(status='open'), record spot Trade
      Place perp limit-IOC short for the spot-filled qty
         on failure → rollback spot via close_spot_limit_ioc
      Reconcile leg sizes (trim spot if perp filled less)
      BREAK (max 1 open per cycle per gateway)

Post-cycle
   ingest_capital_flows  (deposits/withdrawals from venue history)
   prune old rejected_candidates
   futures→spot drain    (keep margin buffer; no-op on unified margin)
   balance snapshot → EquityCurve
```

A loop crash inside any phase is caught by the outer `try/except` in `run_one_cycle`, logged as `Loop iteration error (<mode>): <repr>`, and the cycle moves on.

#### Math

**Important up front**: the bot's entry and exit thresholds (`entry_min_net_apy`, `exit_min_net_apy` in `StrategyConfig`) compare against **net APY** — the annualized profit AFTER worst-case basis cost and round-trip fees — **NOT** raw funding APY. A pair with 80% gross funding APY can still be rejected. (The columns `entry_funding_threshold` / `exit_funding_threshold` are kept in the schema for back-compat but are no longer read; renamed in `config_schema_version` v1 → v2.)

All formulas use the same primitives. **Variables are defined once here.**

| Symbol | Meaning | Source |
|---|---|---|
| `r` | Funding rate as a decimal (e.g. `0.001 = 0.1% per window`) | Predicted next-window rate from `gateway.funding_rates_dict()` |
| `i_h` | Funding interval in hours (4 or 8 typically) | Parsed from the contract metadata |
| `N` | Funding windows per year | `N = 24 × 365 / i_h` |
| `f_w` | Funding window in bps | `f_w = r × 10⁴` |
| `b_e` | Entry basis in bps (signed) | `b_e = (perp_avg − spot_avg) / spot_avg × 10⁴` at the simulated fill |
| `b_l` | Live basis in bps (signed) | `b_l = (perp_mark − spot_mark) / spot_mark × 10⁴` at exit-decision time |
| `m` | Exit-basis buffer multiplier | `cfg.exit_basis_buffer_multiple` (default 3.0) |
| `s_f` / `p_f` | Spot / perp taker fee in bps | Live from the venue's fee API per symbol (cached 1h) |
| `T_in` | Entry threshold (net APY, decimal) | `cfg.entry_min_net_apy` (default 0.20 = 20%) |
| `T_out` | Exit threshold (net APY, decimal) | `cfg.exit_min_net_apy` (default 0.05 = 5%) |

**APY compounding (the only annualization the bot uses):**

```
APY(per_window_bps, i_h) = (1 + per_window_bps / 10⁴)^(24 × 365 / i_h) − 1
```

**Round-trip basis P&L per cycle (always a cost, both signs of entry):**

The worst-case adverse exit assumption is: at exit, the basis has moved further positive by `m × |b_e|`. For long-spot/short-perp, "more positive basis" hurts us (we sell our spot cheap relative to where we have to buy back the perp). So:

```
worst_adverse_swing_bps = m × |b_e|
basis_RT_signed_bps     = − worst_adverse_swing_bps   ← always negative
```

For entry basis +27 bps with m=3: cost = 81 bps. For entry basis −27 with m=3: also 81 bps. The bot doesn't get *credit* for a positive entry basis in the gate — the worst-case eats it. This is conservative by design; real trades that don't hit worst-case outperform.

**Round-trip fees per cycle:**

```
fees_RT_bps = 2 × (s_f + p_f)
            + 2 × (s_f + 5 bps USDC/USDT spot basis)    [if auto-swap fired]
```

**Net profit per funding window:**

```
net_per_window_bps = f_w + basis_RT_signed_bps − fees_RT_bps
                   = f_w − m × |b_e| − fees_RT_bps
```

**Net annualized profit (the actual gate quantity):**

```
net_APY = (1 + net_per_window_bps / 10⁴)^N − 1
```

#### Entry gate

```
OPEN iff   net_APY ≥ T_in
```

The variable name in code is `entry_funding_threshold`, but the gate compares the **net APY** (after worst-case basis + all fees), **not** raw funding APY. A pair with 80% gross funding APY can still fail if fees+basis eat more than 60%.

The Tier-1 pre-filter (in the scan) uses an approximate fee estimate (~5 bps/leg) and assumes zero basis, then compares the same way. It's a cheap eliminator before the book walk; Tier-3 (using live fees + real fill basis) is the real check.

#### Exit gate

Same math as entry, with `b_l` (live basis) instead of `b_e` (fill basis):

```
fwd_net_APY = APY( f_w − m × |b_l| − fees_RT_bps , i_h )
EXIT iff    fwd_net_APY < T_out
```

Interpretation: "If I were opening this trade fresh **right now** at the current funding rate and current basis, would my entry gate approve it at the more lenient `T_out` threshold? If no → close." Symmetric with entry; thresholds are directly comparable.

Two additional exit triggers, evaluated alongside:

- **max_hold**: position age > `cfg.max_hold_hours` (default 72) → exit.
- **stop_loss**: `unrealized_PnL / spot_entry_notional ≤ cfg.stop_loss_pct` (default −0.02 = −2%) → mandatory exit (not deferrable on adverse basis).

Voluntary exits (forward profit, max hold) are **deferred** when live basis exceeds `cfg.max_exit_basis_bps` (default 5.0) — i.e. closing right now would print a basis cost we don't want to lock in. The deferred exit retries every cycle until basis becomes favourable. `stop_loss` and `check_hedge`/`check_market_health` close paths are NOT deferrable.

#### Reservation-aware sizing (inside the walk loop)

Limit-buy reservations on both venues equal `qty × limit_price`. The limit price is `worst_price × (1 + tick_buffer/10⁴)` from the walk, always strictly above the average fill. Sizing `target_qty` purely against `sized_notional / mid_price` overflows reservations on thin books → mid-fill `Balance insufficient! 200004` and partial-fill exposure.

The walk loop converges by clamping `target_qty` on every pass:

```
spot_limit  = spot_worst × (1 + tick_buffer_bps / 10⁴)
perp_limit  = perp_worst × (1 − tick_buffer_bps / 10⁴)

max_qty_by_spot_balance = (spot_leg_free × 0.99) / spot_limit
max_qty_by_perp_balance = (perp_leg_free × 0.99 × leverage) / perp_limit

target_qty = min(target_qty, spot_filled, perp_filled,
                 max_qty_by_spot_balance, max_qty_by_perp_balance)
```

The 0.99 absorbs fee accrual / rounding. Up to 4 passes. Walks re-execute when target_qty shrinks so the profitability gate downstream sees fill prices for the *final* (post-clamp) size, not pre-clamp.

#### Config that applies to this strategy

See [§4 Configuration](#4-configuration) for the full breakdown. The fields used by this strategy:

- Thresholds: `entry_funding_threshold`, `exit_funding_threshold`, `exit_basis_buffer_multiple`, `max_exit_basis_bps`, `stop_loss_pct`
- Sizing: `min_position_pct`, `max_position_pct`, `max_open_positions`, `max_trades_per_day`
- Timing: `max_hold_hours`, `loop_seconds`
- Execution: `entry_tick_buffer_bps`, `exit_tick_buffer_bps`, `perp_leverage` (must be 1)
- Wallet: `auto_transfer_enabled`, `auto_quote_swap_enabled`, `auto_rebalance_threshold`, `futures_buffer_pct`
- Safety: `enforce_hedge_check`, `delisting_check`
- Paper mode: `paper_starting_equity`, `paper_slippage_bps`, `paper_fee_bps`

### 3.2 Cross-venue funding arb [PLANNED]

Trade-type tags reserved in `app/models.py`:
- `binance_kucoin_cross_funding_arb` — spot on one venue, perp on the other
- `ibkr_binance_funding_arb`, `ibkr_kucoin_funding_arb` — IBKR equity / option hedge against a CEX perp

Thesis (when implemented): when two venues have meaningfully different funding rates for the same perp, take the cheap one as the long-funding side and the expensive one as the short-funding side. SOP and math TBD; will require a new wallet model since balances live on different venues, plus venue-vs-venue basis modelling.

### 3.3 Onchain [PLANNED]

Trade-type tag reserved: `onchain_binance_funding_arb` (DEX perp + CEX spot). Chain / protocol still TBD per operator. Will land as a new `Gateway` subclass plus an `app/onchain/<venue>.py` module for RPC / wallet / signing concerns.

---

## 4. Configuration

The bot has **three layers** of configuration. Each layer answers a different question.

| Layer | Who edits | Where | Lifetime |
|---|---|---|---|
| **A. Environment variables** | Operator (one-time) | Coolify env vars | Restart of container |
| **B. Module-level constants** | Developer (in code) | `app/config.py` | Code release |
| **C. `StrategyConfig` row** | Operator (via `/config` UI) | SQLite DB | Edited live, persists across restarts |

### Why all three?

- **A (env vars)** holds secrets (API keys, DB URL, dashboard password, diagnostics token). These can never live in code or DB.
- **B (module constants)** holds **defaults** used to **seed a fresh `StrategyConfig` row** on first run. They are NOT read at runtime after that. They exist so a brand-new deployment with an empty DB starts with sensible thresholds without operator intervention.
- **C (StrategyConfig)** is the **live source of truth** at runtime. Once the row exists, every threshold the bot reads comes from C. Edits on `/config` write to C immediately. Module constants in B are no longer consulted.

This means: **changing values in `app/config.py` does NOT change running-bot behavior** unless you also wipe the `StrategyConfig` row (or change the matching field via `/config`). The constants only set the *initial* default.

### A. Environment variables

See §2.2 for the full list. These are read by `app/config.py`'s `Settings(BaseSettings)` at process start.

### B. Module constants (in `app/config.py`)

Only the seed defaults — none of these are read after the first `StrategyConfig` row is created:

```python
ENTRY_FUNDING_APR    = 0.20    # → cfg.entry_funding_threshold (NET APY threshold)
EXIT_FUNDING_APR     = 0.05    # → cfg.exit_funding_threshold
MAX_HOLD_HOURS       = 72
MAX_OPEN_POSITIONS   = 1
MAX_TRADES_PER_DAY   = 8
LOOP_SECONDS         = 30
STOP_LOSS_PCT        = -0.02
PAPER_SLIPPAGE_BPS   = 5
PAPER_FEE_BPS        = 4
```

The naming `ENTRY_FUNDING_APR` is historical — the value is actually a **net APY** threshold, not a raw funding APR. Renaming the variable is queued; for now treat it as an alias.

### C. `StrategyConfig` (the live runtime config)

A single row in the `strategy_config` table. Edited via the `/config` page.

#### Active fields (currently read by the bot)

| Field | Default | Type | Purpose |
|---|---|---|---|
| `entry_min_net_apy` | 0.20 | float (decimal APY) | Minimum **net APY** to open. Gate at §3.1 entry. (Migrated from `entry_funding_threshold` in v1 → v2.) |
| `exit_min_net_apy` | 0.05 | float (decimal APY) | Forward net APY below this → close. Gate at §3.1 exit. (Migrated from `exit_funding_threshold` in v1 → v2.) |
| `exit_basis_buffer_multiple` | 3.0 | float | `m` in the basis P&L formula. Worst-case exit basis multiplier. |
| `max_exit_basis_bps` | 5.0 | float (bps) | Defer voluntary exits until live basis ≤ this. Stop-loss bypasses. |
| `stop_loss_pct` | -0.02 | float (decimal) | Mandatory exit if `unrealized_PnL / notional ≤` this. |
| `max_hold_hours` | 72 | int | Time-based exit. |
| `max_open_positions` | 1 | int | Across both venues, per mode. |
| `max_trades_per_day` | 8 | int | Soft entry cap per mode. |
| `min_position_pct` | 0.005 | float | Sizing floor as % of equity (0.5%). |
| `max_position_pct` | 0.10 | float | Sizing ceiling (10%). |
| `loop_seconds` | 30 | int | Cycle period. |
| `entry_tick_buffer_bps` | 1.0 | float | Limit-IOC entry price padding above worst-walked-price. |
| `exit_tick_buffer_bps` | 2.0 | float | Same on exit. |
| `perp_leverage` | 1 | int | The only safe value for delta-neutral. |
| `max_perp_leverage` | 1 | int | Hard cap. |
| `auto_transfer_enabled` | true | bool | Pre-trade spot↔futures rebalance for Classic accounts. |
| `auto_quote_swap_enabled` | true | bool | Auto-swap USDT↔USDC pre-trade when a quote wallet is starved. |
| `futures_buffer_pct` | 0.20 | float | Margin buffer kept on futures wallet during post-cycle drain. |
| `enforce_hedge_check` | true | bool | Verify both legs exist on the venue every cycle. |
| `delisting_check` | true | bool | Force-close on market unhealthy. |
| `paper_starting_equity` | 1000 | float (USDT) | Paper virtual capital. |
| `paper_slippage_bps` / `paper_fee_bps` | 5 / 4 | float | Paper synthetic fill costs. |
| `config_schema_version` | 1 | int | Persisted migration cursor; do not edit. |

`max_perp_leverage` is also on the row but is consumed only by `app/main.py` for the "effective APY" display calculation — it doesn't change trading behavior. The actual leverage applied at entry comes from `perp_leverage`.

Master entry/exit toggles per mode live on the `ModeState` table (`mode_state.entry_enabled`, `mode_state.exit_enabled`), **not** on `StrategyConfig`. The DB has `strategy_config.entry_enabled` / `exit_enabled` columns from an earlier design; they're no longer read.

#### Deprecated fields (kept in schema, no longer read)

The following are still in the DB and on the `/config` form for back-compat but are **not** consulted by any decision path:

| Field | Why deprecated |
|---|---|
| `max_entry_basis_bps` | The standalone `basis_dislocated` gate was retired (PR #9). The profitability gate now is the sole economic check. |
| `min_24h_quote_volume` | Scan-time liquidity heuristic. The book walk at sizing-time is the real check (PR earlier in session). |
| `min_order_book_depth_usdt` | Same reason. |
| `depth_band_bps` | Same. |
| `max_position_notional` / `min_symbol_notional` | Replaced by `min_position_pct` / `max_position_pct`. |

| `auto_rebalance_threshold` | Field exists but the pre-trade rebalance logic uses a hardcoded 0.20 USDT threshold. Effectively dead. |
| `strategy_config.entry_enabled` / `exit_enabled` | Superseded by `ModeState`. |
| `taker_fee_bps` (if present in older DBs) | Live per-symbol fee from the venue's fee API is used instead. |
| `min_window_profit_bps` (if present in older DBs) | Old gate replaced by the annualized net APY threshold. |
| `entry_funding_threshold` / `exit_funding_threshold` | Renamed in v2 → `entry_min_net_apy` / `exit_min_net_apy`. Columns kept in schema but no longer read. The legacy names misled operators into thinking they were raw-funding thresholds; they were always NET APY. |

Deprecated fields are kept in the schema (additive-only migration policy) but should be removed from the `/config` form on a future tidy-up PR. Until then they're cosmetic noise.

---

## 5. Wallet model per venue

### 5.1 Binance Portfolio Margin (active)
- Unified pool: one balance per asset, used for both spot and perp.
- Synthesised `futures.<asset>.free` mirrors `spot.<asset>.free`; `futures.<asset>.total = 0` by convention to avoid double-counting in equity sums.
- `is_unified_margin() → True`.
- `transfer_*_to_spot` / `_to_futures` are no-ops on PM (return early).
- Balance fetch: `/papi/v1/balance` → free = `crossMarginFree + umWalletBalance + cmWalletBalance`.

### 5.2 KuCoin Classic (active)
- Three+ spot wallets: `main`, `trade`, `contract`, `margin`, `isolated`, `pool`.
- `is_unified_margin() → False` (returns `self._is_uta`).
- Synthesised `spot.<asset>.free = main + trade` (aggregated). Spot orders only execute against `trade`.
- `consolidate_spot_wallets` sweeps `main` / `margin` / `isolated` → `trade` at the top of every cycle so the abstraction matches reality.
- `futures.fetch_balance({'currency': cur})` called **per-currency** (default returns USDT only). Fixed in PR #15. `wallet_breakdown` had the same bug — fixed in PR #29 so `/api/diagnostics` reports real USDC futures balance instead of always 0.
- **Transfer routing — futures→spot is a TWO-HOP.**
  - **Step 1** (`CONTRACT → MAIN`): `self.futures.transfer(asset, amt, 'CONTRACT', 'MAIN')` via the futures-side legacy `/api/v1/transfer-out`. The spot-side `/api/v3/accounts/universal-transfer` can't drain the futures wallet and returns `112002 "Balance insufficient"` even when `availableBalance` is positive (PR #29).
  - **Step 2** (`MAIN → TRADE`): `self._transfer('main', 'trade', amt)` via spot inner-transfer. KuCoin's `transferOut` ignores the `recAccountType=TRADE` hint and always lands funds in `main`; without the hop, funds wait a cycle for `consolidate_spot_wallets` to sweep them — and that's the window the bot's old drain↔rebalance oscillation used to push them straight back into `contract`. The inline hop guarantees a successful return = "funds are spendable on the spot leg right now".
  - `transfer_spot_to_futures` (IN direction) still uses universal-transfer; that path works.
  - **Idle-cycle oscillation fix (PR #33)**: the pre-trade rebalance now only runs when `candidates_passing > 0`. Without that gate, every idle cycle reshuffled the same dollars through `contract → main → trade → contract …` and produced a ~21k-event/24h log storm of identical transfer failures.
- Identical-error dedup: repeated transfer failures with the same message are throttled via `_TRANSFER_ERROR_CACHE` (same pattern as `_CLOSE_ERROR_CACHE`).
- `pool` (KuCoin Earn) is time-locked; never swept.

### 5.3 KuCoin UTA (not active today)
- Single unified pool. `is_unified_margin() → True`. Transfer methods are no-ops.

### 5.4 Cross-stable USDT ↔ USDC
- Per-quote sizing reads `spot_free_by_q[sq]` and `fut_free_by_q[cq]`. For cross-stable arbs `sq ≠ cq`.
- Auto-swap fires only for same-stable arbs (`sq == cq`) when the relevant pool is below `min_notional` and the other stable has surplus.
- Swap path: `swap_quote(from, to, target)` walks USDC/USDT book, places limit-IOC at worst + tick, with a ±50 bps de-peg guard. Cost is charged to the profitability gate.

---

## 6. Database schema

SQLite default (`bot.db`). Schema in `app/models.py`. Migrations are additive-only `ALTER TABLE ADD COLUMN` calls run at startup (`app/db.py`).

### 6.1 Tables

| Table | Purpose |
|---|---|
| `strategy_config` | Singleton, operator-tuned config. See §4. |
| `mode_state` | Per-mode (paper/live) toggles: `entry_enabled`, `exit_enabled`, `maintenance_mode`. |
| `strategy_state` | Per-(mode, trade_type) toggles: `entry_enabled`, `exit_all_pending`. |
| `positions` | Lifecycle: `open → naked_spot → closed`. Carries entry prices, funding accruals, last error. |
| `trades` | One row per fill. Tagged to a Position. Used by P&L + transactions tab. |
| `bot_events` | Logs at INFO / WARN / ERROR. Source of the `/logs` page and `recent_events` in diagnostics. |
| `rejected_candidates` | Scan rejections — what failed which gate, with reason category. |
| `balance_snapshots` | Per-cycle wallet snapshot per venue. |
| `equity_curve` | Per-cycle equity history per venue. |
| `capital_flows` | Deposits / withdrawals / sub-transfers, ingested from venue history for XIRR. |
| `scan_results` | Per-cycle scan summary. |

### 6.2 Position lifecycle

```
                   (entry path)                            (recovery path)
                        │                                        │
                        ▼                                        ▼
                  ┌──────────┐                              ┌────────────┐
                  │   open   │                              │ naked_spot │
                  └──────────┘                              └────────────┘
                        │                                        │
                exit / close                                  hedge succeeds
                        ▼                                        │
                  ┌──────────┐  ◄────────────────────────────────┘
                  │  closed  │  ◄── sell-back or dust-convert
                  └──────────┘  ◄── stale reconciliation (spot disappeared)
```

`OPEN_STATUSES = ('open', 'naked_spot')` is used by every "currently exposed" query.

**Rendering rule for `naked_spot`**: the perp leg has `perp_entry_price = 0` (placeholder; no perp short was ever filled). The dashboard suppresses the perp-leg detail table for these rows — no fabricated entry/PnL numbers. Spot leg is real.

**Stale reconciliation**: any `naked_spot` Position whose spot wallet balance is gone (sold externally, dust-converted by a prior cycle, Earn redemption) is auto-closed at the top of every live cycle by `recover_phantom_spot`.

### 6.3 Migration policy

- **Additive only.** Never drop columns. Code may stop reading a column; the column stays.
- New columns get a sensible `DEFAULT`. Idempotent.
- One-shot value transforms gated by `config_schema_version` so they run exactly once per row regardless of restarts.

---

## 7. Monitoring & diagnostics

### 7.1 `/api/diagnostics?token=<DIAGNOSTICS_TOKEN>&hours=<1-168>`

Reference: `app/main.py:api_diagnostics`. Auth: `?token=`. Returns `503` if the env var is unset.

Returns JSON with these top-level keys:

```
generated_at_utc       ISO-8601 UTC timestamp
window_hours           the lookback window the caller requested

cycle_health           { last_event_ts, last_event_msg, seconds_since_last_event,
                         error_count, warn_count }
positions              { by_status: {open: N, naked_spot: N, closed: N},
                         open: [...], naked: [...] }
wallets                { <venue>: { <asset>: { <wallet_type>: { free, total } } } }
rejections_grouped     { "<venue>/<mode>": { reason_category: count, ... } }
rejections_total       int
recent_events          [ {ts, level, exchange, mode, msg}, ... ]  ≤ 50 WARN/ERROR
recent_trades          [ {ts, mode, exchange, symbol, venue_leg, side, qty, price, fee}, ... ]
recent_trades_count    int
anomalies              [ {severity, rule, detail}, ... ]
anomalies_count        int
```

### 7.2 Anomaly rules

| Rule | Severity | Trigger |
|---|---|---|
| `no_recent_events` | critical | No `BotEvent` in last 3600s |
| `stale_naked_spot` | warn | A `naked_spot` Position older than 60min |
| `no_trades_despite_scans` | warn | 0 recent trades AND > 20 rejections in window |
| `error_burst` | warn | > 20 ERROR events in window |
| `close_blocked` | warn | Open Position with non-empty `last_close_error` |

### 7.3 Cron + tracker

`.github/workflows/diagnostics.yml` runs every 3h. Required repo secrets: `BOT_URL`, `DIAGNOSTICS_TOKEN`.

The cron pipes the JSON into `.github/scripts/diagnostics_post.py`, which uses the **heartbeat model** (PR #27):

- Locates (or creates) a persistent issue titled `[bot-diagnostics] Tracker`.
- Updates its body to the latest full state.
- Reopens it if anyone closed it.
- **Posts a one-line comment EVERY run** (✅ all-clear or ⚠️ N anomalies + top-3).

The comment fires the GitHub webhook every run — that's how the monitor chat hears about all cycles, not just bad ones.

### 7.4 Monitor chat

Separate Claude session. Reads this doc on every wake-up. Subscribes to the tracker via `subscribe_pr_activity`. Responds per the policy in §10.

---

## 8. Logs & rejection categories

### 8.1 Rejection categories (`rejections_grouped`)

| Category | Meaning | Action when dominant |
|---|---|---|
| `below_threshold` | Tier-1 pre-filter: approx net APY < entry threshold. | None — strategy designed to skip these. |
| `no_spot_market` | Perp's base has no spot pair on the venue. | None — perp-only listing. |
| `insufficient_annualized_profit` | Tier-3 gate: real net APY (after live fees + fill basis) < threshold. | None unless threshold mis-calibrated. |
| `below min position pct` | Sized notional < `min_position_pct × equity`. Wallet starvation. | Inspect `wallets` for stranded funds. |
| `below_min_pct_after_clamp` | Reservation clamp shrunk size below min. | None — genuinely too small. |
| `no_book_depth` | `simulate_fill` returned 0; inner err embedded in the reason. | Inspect inner err (KuCoin limit, BadSymbol, network). |
| `reservation_clamp_zeroed` | Wallet too small even for limit-price reservation. | None. |
| `basis_dislocated` | **DEPRECATED** — gate retired in PR #9. Should be 0. If non-zero, regression. |
| `spot_buy_error: ... Balance insufficient!` | Mid-fill reservation overflow (pre-PR-#10 era) or thin-book partial fill. | After PR #10/14/15 should drop near zero. |
| `spot_buy_error: ... Order size below minimum` | Sizing dropped below venue min after clamp. | Investigate sizing math. |
| `spot_ioc_zero_fill` / `perp_ioc_zero_fill` | Book moved during round-trip. Transient. | None — retries next cycle. |
| `strategy_disabled:<trade_type>` | Operator killed strategy via `/config`. | None unless unintentional. |

### 8.2 Common log patterns (informational)

- `Spot wallet consolidate <asset>: X main→trade` — KuCoin Classic sweep working.
- `Wallet snapshot <q> [Classic|UTA]·split|unified: spot free/total=...; fut free/total=...` — per-cycle wallet state.
- `Pre-trade rebalance skipped: <venue> reports unified margin` — PM/UTA correctly detected.
- `Pre-trade rebalance: X USDT spot→futures (equalize wallets so both legs can fund)` — Classic rebalance working.
- `Scan top <symbol>: predicted rate=X% per Yh → APY=Z%` — top-3 candidate diagnostic per cycle.
- `Phantom spot RESCUED into a hedged position` — recover_phantom_spot Phase 1 success.
- `Phantom spot CLOSED: sold ... → USDT` — recover_phantom_spot Phase 2 sell-back success.
- `Phantom dust detected: ... below venue min` — too small to sell, flagged for dust sweep.
- `Dust sweep CLOSED N naked_spot position(s)` — auto-conversion to BNB/KCS succeeded.
- `Stale naked_spot reconciled: <asset> no longer in spot wallet — marked closed` — stale cleanup fired.

### 8.3 Log patterns that indicate a regression

- `Loop iteration error (<mode>): name '<X>' is not defined` — Python NameError from a missing import. Open a PR. Past examples: `total_funding_income`, `rt_basis_bps`.
- `Reservation clamp on <symbol>` — should NOT appear (clamp moved inside walk loop in PR #12). If it surfaces, regression.
- `basis_dislocated` rejections — should be 0. Regression if non-zero.

---

## 9. Failure modes & recovery

| Failure | Detection | Recovery |
|---|---|---|
| Partial fill under `spot_buy_error` | Pre/post-balance snapshot delta in entry path | Synthesize partial fill, continue to perp leg at smaller qty (PR #14) |
| Naked spot left behind | `recover_phantom_spot` scans every cycle | Hedge with matching perp if profitable, else sell back, else flag as dust (PR #15) |
| Dust below MIN_NOTIONAL | Notional check in recovery | `convert_dust_to_native` → BNB / KCS via venue dust endpoint (PR #21) |
| Stale `naked_spot` (spot disappeared from wallet) | Stale reconciliation at top of recovery | Auto-mark `closed` (PR #30) |
| Wallet starvation | `below min position pct` rejection | `wallet_breakdown` diagnostic surfaces stranded funds |
| Book moves during round-trip | `spot_ioc_zero_fill` / `perp_ioc_zero_fill` | Reject, retry next cycle |
| KuCoin futures→spot drain `112002` / `250001` / wallet oscillation | Post-cycle drain WARN repeating every cycle on idle accounts (21k+/24h) | **Resolved across PR #29 + PR #33.** Three layers: (1) **routing** — switched from spot-side universal-transfer to `self.futures.transfer('CONTRACT', 'MAIN')` via legacy `/api/v1/transfer-out`; (2) **two-hop** — append `main → trade` via spot inner-transfer so funds land where the spot order book can spend them; (3) **idle-cycle gate** — pre-trade rebalance only fires when `candidates_passing > 0`, breaking the drain↔rebalance oscillation. Persistent failures are deduped via `_TRANSFER_ERROR_CACHE`. |
| Symbol drift across ccxt versions | Exit funding miss WARN | Falls back to stale `last_funding_rate`; logged. |
| Loop crash | Outer `try/except` in `run_one_cycle` | Logged as ERROR; loop continues next cycle. |

---

## 10. Response policy (monitor chat)

The cron posts a heartbeat comment on the tracker every run — anomalies or not.

### "✅ all clear" heartbeat

Reply with a **single concise line**: confirm the check happened, summarise state, "no action".

> Cron @ 2026-05-11T18:00Z — all clear. Positions `{open:2, closed:14}`, 8 trades in 24h, errors/warns `0/3`.

### "⚠️ N anomalies" heartbeat

For each anomaly, choose:

| Anomaly | Action |
|---|---|
| Well-understood, no code change (e.g. dust will sweep next cycle) | **Comment** with one-line diagnosis. |
| Known transient (book moved, network blip) | **Skip** if clears next cycle; comment otherwise. |
| Clear code regression (NameError, broken endpoint, latent bug) | **Open PR**. Reference the SYSTEM.md section the fix touches. |
| New venue error code not handled | **Open PR** adding handler + new rejection category in §8.1. |
| Strategy / threshold change | **Ask** the operator before acting. |
| Operator-action-required (e.g. asset with no USDT pair) | **Comment** with the manual step. |
| Anything ambiguous | **Ask** on the thread. |

Combine related anomalies into one reply.

### Never

- Push to `main` directly.
- Skip pre-commit hooks (`--no-verify`).
- Force-push, `git reset --hard`, run destructive shell.
- Touch venue credentials.

### PR workflow

1. Branch: `claude/<short-kebab>`.
2. Small, focused. No drive-by refactors.
3. Smoke: `python -c "import app.main"` + `curl /health` if route surface changed.
4. Use `mcp__github__create_pull_request` + `mcp__github__merge_pull_request` (proxy blocks direct push).
5. **Update SYSTEM.md** in the same PR if behavior changed (§12 makes this binding).

---

## 11. Crons

| Job | Schedule | Trigger | Side effects |
|---|---|---|---|
| Bot's own loop | every `cfg.loop_seconds` (default 30s) | in-process thread per (mode, gateway) | runs the full cycle in §3.1 |
| Diagnostics workflow | `0 */3 * * *` (every 3h) | GitHub Actions cron | hits `/api/diagnostics`, updates the persistent tracker issue body, posts heartbeat comment. Comment fires the webhook to the monitor chat. |

No external crons beyond these.

---

## 12. Doc-update policy

**Binding.** Every PR that changes BEHAVIOR — not just refactors — must update `docs/SYSTEM.md` in the same PR. Specifically:

- New strategy or trade-type → §3.
- New phase / step in the cycle → §3.1 SOP.
- Math change (formula, threshold default, gate logic) → §3.1 math + §0 definitions if a new term is used.
- New venue / wallet type / transfer route → §5.
- New DB column or status value → §6.2.
- New env var → §2.2 + §4.
- New `StrategyConfig` field → §4.
- New `/api/*` endpoint or anomaly rule → §7.
- New rejection category or log pattern → §8.
- New failure mode + recovery → §9.

Reviewers reject PRs that change behavior without updating this doc. When in doubt, add a one-liner — better to over-document than under.

---

## 13. Known fragile / deferred

- **Vultr Auto Backups are NOT enabled.** Single-instance SQLite DB on local NVMe. Loss = entire trade / position / event history. Enable Vultr backups (~$1/mo) or run an off-host backup cron.
- **Per-strategy config split** — today one `StrategyConfig` row serves the one active strategy. When cross-venue / onchain lands, this needs to split into per-strategy rows (or per-trade-type joined config).
- ~~**Naming**: `entry_funding_threshold` / `exit_funding_threshold` are misleading~~ — renamed in PR #32 to `entry_min_net_apy` / `exit_min_net_apy`. Legacy form names still accepted as aliases for one release cycle.
- **Deprecated config fields** (§4) are still on the `/config` form for back-compat. Tidy-up PR pending.
- **Maker-on-exit fee optimization** not implemented. ~30% of exit fees could be saved with post-only-with-timeout-fallback.
- **Symbol mapping drift** across ccxt versions could leave open positions un-lookupable for exit funding refresh. Currently logs a WARN and falls back to stale `last_funding_rate`.
- **Cross-venue + onchain strategies** are roadmap, not implemented.
- ~~KuCoin `futures→spot` drain 112002 / 250001 / oscillation~~ **resolved in PR #29 (routing) + PR #33 (two-hop + idle-cycle gate)** — see §5.2.

---

## 14. Changelog

Append-only. Format: `YYYY-MM-DD · PR# · §sections touched · summary`.

| Date | PR | Sections | Summary |
|---|---|---|---|
| 2026-05-11 | #34 | §0, §3.1 math, §4, §13 | Renamed `entry/exit_funding_threshold` → `entry/exit_min_net_apy` (config_schema_version v1→v2 migration). Removed deprecated form fields (`max_entry_basis_bps`, `min_24h_quote_volume`, `min_order_book_depth_usdt`, `depth_band_bps`). Form accepts both new and legacy field names for one release cycle. |
| 2026-05-11 | #33 | §3.1, §5.2, §9, §13 | Break KuCoin drain↔rebalance oscillation. (1) Gate pre-trade rebalance on `candidates_passing > 0` (no point equalising wallets when there's no trade to fund). (2) `transfer_futures_to_spot` is now a two-hop: futures `CONTRACT → MAIN` via `transferOut`, then spot `MAIN → TRADE` via inner-transfer. Funds land where the spot order book can spend them without waiting a cycle for `consolidate_spot_wallets`. |
| 2026-05-11 | #32 | tooling | `diagnostics_post.py`: post heartbeat comment **before** body edit, make body edit non-fatal, trim payload. Body-too-large 504s no longer block the comment, which is what fires the monitor chat webhook. |
| 2026-05-11 | #31 | rewrite | SYSTEM.md v1.0 — full rewrite after operator audit. Definitions upfront, per-strategy SOP + math, config layers explained, deprecated fields called out, exit-logic regression `rt_basis_bps` → `rt_basis_signed_bps` fixed alongside. |
| 2026-05-11 | #29 | §5.2, §9, §13 | KuCoin futures→spot drain uses the futures-side `transferOut` endpoint (legacy `/api/v1/transfer-out`) instead of the spot-side universal-transfer (which can't see the futures wallet). `wallet_breakdown` USDC contract under-report fixed. Identical-error dedup via `_TRANSFER_ERROR_CACHE`. Resolved the 112002 deferred item. |
| 2026-05-11 | #30 | §6.2, §9 | Don't render fake perp leg for `naked_spot`; auto-close stale naked rows. |
| 2026-05-11 | #27 | §7, §10, §11 | Heartbeat-model diagnostics tracker. Monitor always knows the cron ran. |
| 2026-05-11 | #26 | §2 | Vultr specs + KuCoin permissions clarification + backup-risk callout. |
| 2026-05-11 | #25 | §2, §4 | Operator-provided setup details rolled in. |
| 2026-05-11 | #24 | new | SYSTEM.md v0.1 first cut. |
| 2026-05-11 | #21 | §3.1, §9 | Auto-convert dust to BNB/KCS via venue dust endpoints. |
| 2026-05-11 | #20 | §8.3, §9 | Fix `total_funding_income` NameError. Silence dust spam. LDUSDT filter. Workflow label fallback. |
| 2026-05-11 | #18 | §7 | `/api/diagnostics` endpoint + GitHub Actions cron + tracker. |
| 2026-05-11 | #17 | §6.2, §8 | Naked positions are first-class in dashboard + transactions. |
| 2026-05-11 | #16 | §3.1 math | Charge auto-swap fees in profitability gate. |
| 2026-05-11 | #15 | §5.2, §9 | Hedge phantom spot via perp when profitable. KuCoin futures per-currency fetch. |
| 2026-05-11 | #14 | §3.1 SOP, §9 | Recover orphaned spot positions + partial-fill detection. |
| 2026-05-11 | #13 | §5.2 | KuCoin sweep margin/isolated + `wallet_breakdown` diagnostic. |
| 2026-05-11 | #12 | §3.1, §5, §6 | Audit cleanups: reservation clamp in walk loop, exit funding refresh, sign math, migration v1, dead-config purge. |
| 2026-05-11 | #11 | §8 | `below_threshold` log shows net APY (the number actually compared). |
| 2026-05-10 | #10 | §3.1 math | Reservation-aware target_qty clamp. |
| 2026-05-10 | #9 | §3.1, §4 | Dropped `basis_dislocated` gate; profitability-only economic check. |
| 2026-05-10 | #8 | §3.1, §8 | KuCoin book-walk limit fix, sign-aware basis, funding APY diagnostic. |
| 2026-05-10 | #7 | §5.2 | KuCoin Classic spot-wallet consolidation. |

(Older history in `git log`.)

---

> **For the monitor chat:** Always read this doc from the latest `main` before judging anomalies. The definitions (§0), strategy SOP + math (§3), rejection categories (§8), failure modes (§9), and response policy (§10) are your operating manual.
