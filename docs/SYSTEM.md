# AutoTrader_Codex — System SSOT

The single living source of truth for what this bot does and how it works. Every behavior-changing PR updates the relevant section in the same commit. The diagnostics-monitor chat re-reads this on every wake-up before judging anomalies; any new dev reads it once to onboard.

> Status: **v1.0** — 2026-05-11 rewrite after operator audit.

## How to read this doc

This is a **specification**, not a description of the running code. It tells a developer or operator what the bot DOES and what a future rewrite must preserve. Code identifiers (file paths, class names, function names) are intentionally absent — they belong in the implementation, not the spec.

Suggested reading order on first pass:

- **Brand-new to the bot?** §1 (Purpose) → §0 (Definitions, all sections) → §3 (Strategy SOP + math) → §5 (UI). The strategy section assumes every term in §0.
- **Operating it day-to-day?** §0.5 (money quantities — what the dashboard means) → §5 (UI tour) → §8 (diagnostics) → §10 (failure modes).
- **Modifying it?** §3 (strategy) + §4 (configuration) + §13 (doc-update policy) + §16 (learnings — read this before every PR).
- **Rewriting it?** §17 (rewrite plan) is the runbook. §16 is the regression catalogue.

If a sentence in any section uses a term, that term is in §0. If it isn't, **the section is wrong, not the reader** — please open a PR adding the definition.

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
- [5. UI](#5-ui)
- [6. Wallet model per venue](#6-wallet-model-per-venue)
- [7. Database schema](#7-database-schema)
- [8. Monitoring & diagnostics](#8-monitoring--diagnostics)
- [9. Logs & rejection categories](#9-logs--rejection-categories)
- [10. Failure modes & recovery](#10-failure-modes--recovery)
- [11. Response policy (monitor chat)](#11-response-policy-monitor-chat)
- [12. Crons](#12-crons)
- [13. Doc-update policy](#13-doc-update-policy)
- [14. Known fragile / deferred](#14-known-fragile--deferred)
- [15. Changelog](#15-changelog)
- [16. Learnings](#16-learnings)
- [17. Rewrite plan](#17-rewrite-plan-core--ui-from-scratch)

---

## 0. Definitions

Every term used downstream in the doc lives here. Grouped to build up from primitives to compound concepts; later groups assume the earlier ones. **If a section uses a term not defined below, the section is wrong — fix the section.**

### 0.1 Trading primitives

| Term | Definition |
|---|---|
| **Spot market** | The "right now" market for an asset paired with a quote (e.g. `BTC/USDT`). Buying spot = you now own the asset. Selling spot = you no longer own it and have the quote currency instead. No leverage, no funding payments, no expiry. |
| **Perpetual futures (perp)** | A derivative contract that tracks the spot price of an asset (e.g. `BTC/USDT:USDT` tracks BTC vs USDT) but has **no expiry date**. Traders can be **long** (profit if price rises) or **short** (profit if price falls). The contract is held open indefinitely; the platform forces buyers and sellers to keep the perp's price near the spot price by charging periodic **funding payments**. |
| **Funding rate** | The periodic payment between perp longs and shorts that keeps the perp price anchored to spot. Quoted as a percentage per **funding window**. **Positive funding** = longs pay shorts (perp is trading above spot); **negative funding** = shorts pay longs. |
| **Funding window** | The cadence of funding settlements. Typically 4 hours or 8 hours, set per contract by the venue. A 0.01% funding rate at an 8h window means the short side receives 0.01% of the position's notional from the long side every 8 hours. |
| **Base asset** / **Quote asset** | In `BTC/USDT`, **BTC** is the base (what you're buying / selling) and **USDT** is the quote (the currency you pay or receive). The pair's price is quoted in units of QUOTE per unit of BASE. |
| **Stablecoin** (or **stable**) | A token designed to track $1 USD. The bot trades USDT and USDC. Their prices vs USD are usually ~$1 ± 5 bps (0.05%) but can drift. The bot treats USDT face-value as 1 USD and prices USDC at the live USDC/USDT mid for equity reporting. |
| **Long position** | You've bought the asset (or perp) and will profit if the price rises. |
| **Short position** | You've sold an asset you don't own (in the case of perps, via the contract's short side). You profit if the price falls. |
| **Delta-neutral** | Net exposure to the underlying asset's price moves is zero. If price goes up, gains on the long leg cancel losses on the short leg. The bot is **always** delta-neutral by construction: long spot + short perp on the same base, same quantity. |
| **Basis** | The price gap between perp and spot, normalised: `(perp_price − spot_price) / spot_price`, expressed in basis points (bps). **Positive basis** = perp is trading at a premium to spot. Example: spot BTC = $50,000, perp BTC = $50,050 → basis = +10 bps. |

### 0.2 Order types

| Term | Definition |
|---|---|
| **Market order** | "Buy/sell now at whatever price the book offers." Fills immediately, but at potentially poor prices on thin books. **The bot never uses market orders** — too much slippage risk on thinly-traded pairs. |
| **Limit order** | "Buy/sell at price X or better." Fills against existing orders that cross your limit. Can rest on the book waiting if nothing crosses. |
| **IOC** (Immediate-Or-Cancel) | A time-in-force flag. Order fills whatever depth it can against existing book at the moment of arrival, then immediately cancels any unfilled remainder. **Never** leaves a resting order. |
| **Limit-IOC** | The bot's only order type. Combines limit + IOC: "fill against the book up to my limit price, right now, and cancel anything left." Predictable cost, predictable fill quantity, no resting-order risk. |
| **Taker fee** | The fee charged when an order crosses the existing book (i.e. removes liquidity). Limit-IOC orders are always takers. Typically 0.06–0.10% per fill on these venues. |
| **Maker fee** | The fee (sometimes a rebate) for an order that rests on the book waiting to be crossed. The bot does NOT use maker orders — see §16 L11 / L21. |
| **Reservation** | When you submit a limit-buy for `qty × limit_price`, the venue immediately **reserves** that much cash from your wallet. If your wallet's free balance is less, the venue rejects the order mid-fill. Crucial because the reservation uses the LIMIT price (always worse than the average fill price), not the average. |
| **Tick size** | The minimum price increment a venue accepts for an order on a given symbol. E.g. 0.0001 USDT. Orders priced off-tick are rejected. |
| **MIN_NOTIONAL** | The venue's per-symbol minimum order value in quote currency. Below this, the venue rejects the order. Roughly $5 on Binance, $1 on KuCoin. Dust below this can't be sold through normal orders — must use the venue's dedicated dust-conversion endpoint. |

### 0.3 The bot's strategy (long-spot / short-perp funding arbitrage)

| Term | Definition |
|---|---|
| **Long spot + short perp** | The bot's only active structure. Buy the base asset on spot; simultaneously short the same quantity of that asset's perp. Net price exposure = 0. The position earns funding payments every funding window while the perp's funding rate is positive. |
| **Entry basis** | The basis at the moment we open: `(perp_sell_fill_price − spot_buy_fill_price) / spot_buy_fill_price × 10000`, in bps. Positive entry basis = we sold the perp leg at a premium relative to where we bought the spot leg = we pocketed that gap as entry profit. |
| **Worst-case adverse exit basis** | A conservative assumption: "by the time we close, the basis will have moved against us by `m × |entry_basis|` bps", where m is a multiplier (default 3.0). For long-spot / short-perp, "adverse" means basis moves further positive — we sell our spot cheap relative to where we have to buy back the perp. |
| **Position leg** | Either the spot side or the perp side of a single delta-neutral position. Each position has two legs that should be equal in absolute quantity at all times. |
| **Naked leg** | A leg that lost its counterpart (e.g. spot leg filled but perp leg failed → the spot is "naked-long"). Naked legs are unhedged and exposed to price moves; the bot tries to recover them every cycle. |
| **Naked spot** | A spot holding the bot owns but for which it has no matching perp short. Persisted as a position with status `naked_spot`. Created when a partial fill under an error response left a spot position with no perp hedge. See §10. |

### 0.4 Position lifecycle

| Term | Definition |
|---|---|
| **Position** | A row in the bot's database recording one delta-neutral pair (one spot leg + one perp leg) on one venue under one strategy. Has a status: `open`, `naked_spot`, or `closed`. |
| **Open position** | Both legs are live on the venue and the position is earning funding. The dashboard's "Open positions" table shows these. |
| **Naked-spot position** | Spot leg exists, perp leg does not. Sub-state of "open" for accounting (counts toward exposure) but flagged for recovery on the next cycle. Once hedged or sold back, the row transitions out of `naked_spot`. |
| **Closed position** | The position has been fully closed (both legs flat) and the realized P&L is locked in. Visible in the dashboard's "Closed positions" history. |
| **Currently exposed** | The combined set `{open, naked_spot}` — every "currently exposed" query in the bot uses this set. |

### 0.5 Money quantities (what the dashboard's KPI cards show)

This is the operator's primary "how am I doing?" view. Every number here has a precise definition.

| Term | Definition |
|---|---|
| **Notional** | The dollar value of a position, computed as `quantity × price`. The bot uses `quantity × spot_entry_price` for entry notional and `quantity × current_spot_price` for current notional. A 0.05 BTC position with BTC at $50,000 has notional $2,500. |
| **Portfolio equity** (or just **equity**) | The total dollar value of every asset the bot's account holds across every wallet on every venue, valued at current market prices. Computed by summing: (a) the operator's USDT balances at face value, (b) USDC balances at the live USDC/USDT mid, (c) every non-stable base asset balance at its current spot price. Equity changes second-to-second as prices move; the dashboard's "Current equity" KPI refreshes per cycle. |
| **Free deployable** | The portion of equity NOT currently committed to an open position — i.e. cash that the bot could route into a new trade right now. Spot positions count as "committed" because their value moves with price; only idle stablecoin balances count as free deployable. Computed as `idle USDT + idle USDC × USDC/USDT rate`. |
| **Net injected capital** | The total dollars the operator has deposited into the account, minus dollars withdrawn, since the bot's inception. **Does NOT include trading P&L.** Ingested from each venue's deposit / withdrawal / sub-transfer history. Used as the baseline for total-PnL and XIRR calculations. |
| **Mark-to-market (MTM)** | Valuing a position at the current market price rather than entry price. Standard accounting convention — even though no trade has been executed, the position's "what would I have if I closed right now?" value matters. |
| **Realized P&L** (or **trade PnL**) | Profit / loss already locked in by closed positions. Computed across every closed trade's entry-vs-exit prices. Excludes funding income (tracked separately). |
| **Unrealized P&L** (or **MTM PnL**) | Profit / loss on currently-open positions, valued at current market prices. Becomes realized when the position closes. |
| **Funding income** | The cumulative funding payments the open shorts have received from open longs across the position's lifetime. Tracked per-position and summed for the portfolio total. On the dashboard's "Total PnL" breakdown, this is a separate line item from trade PnL. |
| **Total PnL** | `Current equity − Net injected capital`. The bottom-line "have I made or lost money since I deposited?" number. Decomposes into `trade_PnL + funding_income + unrealized_MTM_PnL`. |
| **Total fees** | The sum of all taker fees paid across every spot and perp leg of every trade the bot has executed. Visible on the dashboard with average fee per transaction as a % of notional and a per-(venue, leg) breakdown. |
| **XIRR** | Internal Rate of Return computed against the actual deposit / withdrawal timestamps. Annualised. Handles irregular capital flows correctly (a simple % return on net-injected doesn't). Needs at least 7 days of history and at least one capital flow to be meaningful. |

### 0.6 Annualization

The bot uses APY everywhere it talks about a yearly return. APR appears only as a historical name in some legacy variables; it is never the actual math the bot does.

| Term | Definition |
|---|---|
| **APR** (simple-interest annualization) | `period_rate × periods_per_year`. Linear, no compounding. *Not used in this codebase, despite the name appearing in some legacy variable names.* |
| **APY** (compounded annualization) | `(1 + period_rate)^periods_per_year − 1`. Compounds the period yield into yearly. This is what every threshold and every dashboard rate in the bot uses. |
| **Worked example** | A perp pays 0.01% funding every 8 hours. Periods/year = 24×365 / 8 = 1095. APR = 0.01% × 1095 = **10.95%**. APY = (1.0001)^1095 − 1 = **11.57%**. The bot uses 11.57%. Same rate at 4-hour funding: APR = 21.9%, APY = **24.50%**. The funding window matters a lot when annualizing. |
| **Net APY** | The bot's headline performance metric per candidate. The annualization of **net** profit (funding income + signed basis P&L − round-trip fees − optional stablecoin-swap costs). Every threshold in the configuration (`entry_min_net_apy`, `exit_min_net_apy`) is expressed in this unit. **NOT the raw funding APY.** A pair with 100% raw funding APY can fail the entry gate if fees + worst-case basis cost more than (100% − operator's threshold). |

### 0.7 Venue account models

Different venues bundle balances differently. The bot abstracts over this but the differences matter for the wallet model in §6.

| Term | Definition |
|---|---|
| **Account** | The top-level identity on a venue. Operator may have a master account and several sub-accounts. The bot lives in one sub-account per venue. |
| **Sub-account** | A bookkeeping isolation within an account. Different sub-accounts can run different strategies / API keys; balances don't pool unless explicitly transferred. |
| **Master account** | The parent that can create / fund / restrict sub-accounts. |
| **Wallet type** | A sub-bucket within an account. On simple venues there's one "spot wallet" + one "futures wallet". On more complex venues (KuCoin Classic) the spot side splits into `main` (deposits land here), `trade` (where spot orders execute), `margin` / `isolated` (for margin trading), and `pool` (Earn / yield products). |
| **PM** (Portfolio Margin, Binance) | A unified-margin account mode where the spot wallet, USDM-futures wallet, and CM-futures wallet all draw from a single collateral pool. Orders route through a separate set of API endpoints (`/papi/v1/*`). This bot's Binance integration assumes PM unconditionally. |
| **UTA** (Unified Trading Account, KuCoin) | KuCoin's equivalent of PM — single unified pool across spot + futures. Not active for this bot's KuCoin sub-account; it runs in **Classic** mode. |
| **Classic** (KuCoin non-UTA) | The default KuCoin mode. Funds split across multiple wallet types (main, trade, contract, margin, isolated, pool) and the bot has to physically transfer between them — see §6.2. |

### 0.8 Strategy + execution terms

| Term | Definition |
|---|---|
| **Strategy** | A trading approach implemented in the bot. Today only one is active: same-venue funding arbitrage (long spot + short perp on the same venue). Each active venue + strategy combination gets its own runtime config row. |
| **Trade type** | The identifier for a (strategy, venue) pair, e.g. `binance_same_venue_funding_arb`. Every Position and Trade row carries the trade type that produced it. |
| **Cross-stable arb** | A funding-arb candidate where the perp's quote currency differs from the spot's quote currency — e.g. spot `DOGE/USDT` + perp `DOGE/USDC:USDC`. The bot funds the spot leg from the spot-quote wallet and the perp leg from the perp-quote wallet independently. |
| **Same-stable arb** | Spot and perp share the same quote (both USDT or both USDC). If the wallet for that single quote is empty but the OTHER stable has surplus, the bot can auto-swap USDT↔USDC to fund the trade. |
| **Mode** | The bot runs two parallel execution paths: **paper** (synthetic fills against real venue prices — no orders sent, no real money at risk) and **live** (real orders). Both run every cycle on every venue. Data is segregated by a `mode` tag on every database row. |
| **Cycle** (or **loop iteration**) | One pass of the bot's three-phase work: safety checks (Phase A) → position exits (Phase B) → new entries (Phase C) → post-cycle bookkeeping. Runs every ~30 seconds (configurable). |
| **Gate** | A binary decision point that admits or rejects a candidate trade based on a rule. The bot has three tiers (below). |
| **Tier-1 gate** | Cheap pre-filter run during the venue-wide funding-rate scan. Uses approximate fees and assumes zero basis. Rejects candidates whose net APY estimate is below the entry threshold before any expensive work happens. |
| **Tier-2 gate** | Book-walk simulation. The bot replays the venue's actual order book at the bot's actual sizing and confirms both spot and perp legs can fill cleanly. Returns real avg + worst-fill prices. |
| **Tier-3 gate** | The real economic check. Uses the actual fill prices from Tier-2, live per-symbol taker fees from the venue's fee API, signed basis P&L with worst-case adverse exit, plus a stablecoin-swap surcharge if needed. The candidate must clear net APY ≥ entry threshold to reach order placement. |
| **Candidate** | A perp the scan considers a potential trade for this cycle. Becomes a candidate after passing Tier-1 and having a viable spot pair. Top candidates by net APY then face Tier-2 and Tier-3. |
| **Sized notional** | The dollar amount of equity the bot wants to commit to this trade, computed before the book walk. Bounded below by `min_position_pct × equity` and above by `max_position_pct × equity`, then further capped by the smaller of the two leg wallets' free balances. |
| **Wallet cap** | The hard ceiling on a trade's size imposed by available cash: `min(spot_leg_free, perp_leg_free) × safety_factor` (safety factor ≈ 0.97 covers limit-vs-mid price spread + fees). The bot will never try to commit more than this even if `max_position_pct × equity` is larger. |
| **Target quantity** (target_qty in formulas) | The base-asset quantity the bot is trying to fill. Initially `sized_notional / spot_mid_price`; clamped down in the book-walk loop by both fillable depth and reservation limits at the final limit price. |
| **Reservation** | When a limit-buy is placed for `qty × limit_price`, the venue immediately reserves that much from the trade wallet. The clamp ensures `qty × limit_price ≤ wallet_free × safety_factor` so the venue never rejects mid-fill. See §16 L07. |
| **Safety factor** | A multiplier (~0.99) applied to free-balance ceilings to absorb fee accrual, rounding, and last-millisecond balance shifts. Trades a tiny bit of headroom for reservation-rejection insurance. |
| **Mandatory vs voluntary exit** | **Voluntary** exits (forward profitability dropped, max-hold reached) are deferred for one cycle if closing right now would print an extra cost (unfavourable live basis). **Mandatory** exits (stop-loss, hedge integrity, market unhealthy) close immediately regardless. |

### 0.9 Operational concepts

| Term | Definition |
|---|---|
| **Diagnostics endpoint** | An auth-gated JSON endpoint (§8) that returns a structured snapshot of cycle health, positions, wallets, recent events, recent trades, and rule-based anomalies. Polled by an external cron every 3 hours and by humans / monitor agents on demand. |
| **Anomaly** | A rule-based flag produced by the diagnostics endpoint when the bot's state diverges from healthy steady-state — e.g. no events in the last hour, naked-spot older than 1 hour, error burst, etc. |
| **Heartbeat tracker** | A persistent GitHub issue that the diagnostics cron updates every 3 hours regardless of anomaly state. Posting a comment per run guarantees a webhook fires every cycle so the monitor chat can confirm "the bot's still alive". |
| **Monitor chat** | A dedicated Claude session subscribed to the heartbeat tracker. Reads this doc on every wake-up before judging anomalies. Responds inline on the tracker with one-line acknowledgements on clean runs, full diagnosis on anomaly runs, or PRs when it detects a code regression. |

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
| Auto Backups | **NOT ENABLED** ⚠ (see §14) |

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
- The bot's Binance gateway assumes Portfolio Margin unconditionally — every order routes through the PM endpoints. Switching the account off PM mode will break sizing / transfer paths.

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

Roadmap target. No code yet. Will land as new venue gateways with onchain-specific concerns (RPC, wallet, signing) isolated from the trading logic.

---

## 3. Strategies

This bot is multi-strategy by design — each strategy has its own SOP and per-strategy config (see §4). **Today only same-venue funding arbitrage is active** (one instance per active venue: Binance, KuCoin). When cross-venue and onchain land, this section grows.

### 3.1 Same-venue funding arbitrage [ACTIVE]

**Identifier:** `binance_same_venue_funding_arb`, `kucoin_same_venue_funding_arb` (one strategy instance per active venue).

#### Thesis (plain English)

Perpetual futures don't expire, so the venue needs a mechanism to keep the perp's price close to the spot price. That mechanism is the **funding rate** — every funding window (typically 4 or 8 hours), one side of the perp's order book pays the other side. When the perp is trading above spot, longs pay shorts; when below, shorts pay longs.

This bot's only active trade: pick a perp paying high positive funding, **buy the base asset on spot** and simultaneously **short the same base on the perp** in equal quantity. Net price exposure is zero (whatever spot does, the short perp does the opposite), so the position doesn't care if the underlying goes up or down. Each funding window, the short perp **receives** the funding payment from the long perp side. The position holds until either the funding rate falls below a threshold or 72 hours pass.

#### Where the money comes from (and where it leaks)

```
LONG spot   (own the base asset; hedge against the perp)
SHORT perp  (collect funding from longs as long as funding rate > 0)
```

Returns come from:

1. **Funding income** every funding window. Dominant on hot pairs — a perp paying 0.6% per 8h compounds to ~700% APY if it persists.
2. **Entry basis kicker** when the perp sells at a premium to where the spot buys. If the bot fills spot at $1.00 and perp at $1.005, that 50 bps gap is locked-in profit on entry.

And leaks:

3. **Worst-case adverse basis swing** between entry and exit. The bot conservatively assumes basis moves against it by `m × |entry_basis|` (m default 3.0) by close time. Subtracts that from the funding income.
4. **Round-trip taker fees** — 2 spot legs (entry + exit) + 2 perp legs (entry + exit) + 2 USDC↔USDT swap legs if a stablecoin swap was needed.

The bot's gate compares **net APY** (funding − adverse basis − fees, all annualized) against the operator's threshold (default 20% net APY). Raw funding APY is NOT the threshold.

#### Concrete example

Operator sets entry threshold = 20% net APY. The scanner finds `XYZ/USDT:USDT` paying 0.025% funding per 4h, with a +30 bps entry basis (we sell the perp 30 bps above where we buy spot).

- Funding per 4h = 0.025% = 2.5 bps
- Worst-case basis cost (m=3, entry=30 bps) = −90 bps over the round trip
- Round-trip fees (2 × spot + 2 × perp, ~6 bps each) ≈ −24 bps
- Net per 4h window = 2.5 − 90/N − 24/N bps, where N = funding-windows-held expected count

For the **first** window alone, the basis + fees overwhelm the funding. But the bot annualizes the *per-window* net: 2.5 − 90 − 24 = −111.5 bps per 4h, which is a deeply negative APY. The gate rejects this candidate.

For a higher-funding candidate at 0.5%/4h:
- Funding per 4h = 50 bps
- Adverse basis (m=3, entry=30 bps) = −90 bps total round-trip cost
- Fees ≈ −24 bps
- Net per 4h window = 50 − 90 − 24 = ... actually wait — the basis and fee costs are *one-time* round-trip costs, not per-window. Better formulation:
  - Per-window net = funding − (round-trip basis + fees) / N
  - Annualized (compounded) = (1 + net_per_window_bps / 10000)^(24×365/i_h) − 1
  - For 50 bps funding / 4h, with one-time costs of 90+24 = 114 bps amortised over (say) 18 windows held = 6.3 bps/window: net = 50 − 6.3 = 43.7 bps per 4h → APY ~ 16,000% (compounded). Cleared the gate.

The bot's actual math is more conservative than this rough sketch — it treats the worst-case basis as a per-window cost (not amortised over expected hold) so the gate stays robust to early exits. See the formulas below.

#### SOP per loop iteration

Loop period: globally configured (default 30s). Runs separately for paper and live mode, on each active venue.

```
Phase A — Safety (live only)
   For each open position:
      market-health check     → if market is delisted/halted, force-close both legs
      hedge-integrity check   → if one leg has disappeared from the venue, close the surviving leg
   Phantom-spot recovery (sweep wallet, look for orphaned spot holdings):
      ① Stale reconciliation   any naked-spot position whose underlying spot
                                balance is no longer in the wallet → mark closed
      ② For each non-stable spot asset present in the wallet with notional ≥
         tracking floor (≈ $0.10):
           - if notional < venue MIN_NOTIONAL → persist as naked-spot dust;
             skip recovery this cycle, defer to dust sweep
           - else: persist as naked-spot, then either
               Phase 1: try to hedge with the matching perp (if forward
                        profitability gate passes at current funding); or
               Phase 2: sell the spot back to the quote stablecoin via
                        limit-IOC
      ③ Dust sweep: convert all naked-spot-dust positions to the venue's
         native fee token (BNB / KCS) via the venue's dust-conversion
         endpoint; mark each converted position closed

Phase B — Exits (when exit is enabled for this mode/strategy)
   Fetch fresh predicted funding rates per venue.
   For each open position:
      update last-known funding rate + funding interval
      compute forward-looking net APY at LIVE funding + live basis (math below)
      exit triggers (whichever fires first):
         forward_profit_below_threshold   (forward net APY < exit threshold)
         max_hold                         (age > max-hold hours)
         stop_loss                        (mark-to-market PnL / entry notional ≤ stop-loss-pct)
                                          → MANDATORY (not deferrable below)
      Voluntary exits (forward-profit, max-hold) DEFER for one cycle if the
      live basis is currently unfavourable for closing (would print extra cost).
      Mandatory exits (stop-loss, hedge integrity, market unhealthy) close
      immediately regardless.

Phase C — Entries (when entry enabled for this mode/strategy AND at-capacity check passes)
   Scan funding rates across every perp on the venue:
      Tier-1 pre-filter (cheap, per-pair):
         approx_net_apy = annualize(funding − approx_round_trip_fees)
         REJECT below_threshold if approx_net_apy < entry threshold
      Spot-pair existence check (force-reload markets cache if missing).
      Rank passing candidates by funding APY descending.
   Wallet prep (live only):
      Consolidate spot wallets (KuCoin Classic: sweep all sibling spot
         buckets into the trade wallet so the abstraction matches the
         venue's actual order-matching surface)
      Pre-trade rebalance — split-wallet venues only, AND only when at
         least one candidate passed the scan (no point equalising
         wallets on an idle cycle)
      Auto-swap USDT↔USDC if the top-candidate is a same-stable arb
         and the wallet for that stable is starved
   For each candidate (top N by APY):
      Skip if base asset is already held (any open or naked position).
      Iterative book-walk loop (up to 4 passes):
         Parallel simulate fill on spot (buy) + perp (sell) at current target qty
         Compute provisional limit prices (worst-fill ± tick buffer)
         Clamp target_qty by:
            min(book-fillable qty,
                what fits in spot leg's free balance at spot limit price,
                what fits in perp leg's free balance × leverage at perp limit price)
         Break when target_qty stops shrinking
      Tier-3 profitability gate (full math below).
         REJECT insufficient_annualized_profit if net APY < entry threshold.
      Place spot limit-IOC at the worst-walked-price + tick buffer.
         Pre-snapshot the base-asset spot balance; on order exception, re-read
         balance — if any quantity actually filled, synthesize a partial fill
         dict and continue to the perp leg at the smaller actual size.
      Persist position with status = open; record the spot fill as a Trade.
      Place perp limit-IOC short for the spot-filled qty.
         On exception or zero fill: roll back the spot leg (sell back); record
         the rollback as a Trade; reject with perp_short_error.
      If perp filled less than spot, trim the spot leg to match.
      BREAK after one successful open (max 1 open per cycle per venue).

Post-cycle
   Ingest venue capital-flow history (deposits / withdrawals / sub-transfers).
   Prune old rejected-candidate rows.
   Futures→spot drain (split-wallet venues): keep a margin buffer on the
      futures wallet, sweep the rest to spot. No-op on unified margin.
   Take a balance snapshot, append to the equity curve.

Crash handling
   Any exception inside the cycle is caught at the outer boundary, logged as
   "Loop iteration error", and the loop continues on the next iteration.
```

#### Math

**Important up front**: entry and exit thresholds compare against **net APY** — the annualized profit AFTER worst-case basis cost and round-trip fees — **NOT** raw funding APY. A pair with 80% gross funding APY can still be rejected.

Variables (defined once, used everywhere):

| Symbol | Meaning | Source |
|---|---|---|
| `r` | Funding rate as a decimal (e.g. `0.001 = 0.1% per window`) | Predicted next-window rate from the venue |
| `i_h` | Funding interval in hours (typically 4 or 8) | Read from the contract metadata |
| `N` | Funding windows per year | `N = 24 × 365 / i_h` |
| `f_w` | Funding window in bps | `f_w = r × 10⁴` |
| `b_e` | Entry basis in bps (signed) | `b_e = (perp_avg_fill − spot_avg_fill) / spot_avg_fill × 10⁴` |
| `b_l` | Live basis in bps (signed) | `b_l = (perp_mark − spot_mark) / spot_mark × 10⁴` at exit-decision time |
| `m` | Exit-basis buffer multiplier | Per-strategy config (default 3.0) |
| `s_f` / `p_f` | Spot / perp taker fee in bps | Live from the venue's fee API per symbol |
| `T_in` | Entry threshold (net APY, decimal) | Per-strategy config (default 0.20 = 20%) |
| `T_out` | Exit threshold (net APY, decimal) | Per-strategy config (default 0.05 = 5%) |

**APY compounding (the only annualization the bot uses):**

```
APY(per_window_bps, i_h) = (1 + per_window_bps / 10⁴) ^ (24 × 365 / i_h) − 1
```

**Round-trip basis P&L per cycle (always a cost, both signs of entry):**

The worst-case adverse exit assumption: at exit, the basis has moved further positive by `m × |b_e|`. For long-spot/short-perp, "more positive basis" hurts (we sell our spot cheap relative to where we have to buy back the perp).

```
worst_adverse_swing_bps = m × |b_e|
basis_RT_signed_bps     = − worst_adverse_swing_bps      ← always negative
```

For entry basis +27 bps with m=3: cost = 81 bps. For entry basis −27 with m=3: also 81 bps. The bot does NOT credit a positive entry-basis kicker in the gate — the worst-case assumption eats it. This is conservative by design; real trades that don't hit worst-case outperform.

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
net_APY = (1 + net_per_window_bps / 10⁴) ^ N − 1
```

#### Entry gate

```
OPEN iff   net_APY ≥ T_in
```

The Tier-1 pre-filter (in the scan) uses the same formula with approximate fees (~5 bps/leg) and zero basis. Cheap eliminator before the book walk. Tier-3 (using live fees + real fill basis from the book walk) is the binding check.

#### Exit gate

Same math as entry, with `b_l` (live basis) replacing `b_e` (fill basis):

```
fwd_net_APY = APY( f_w − m × |b_l| − fees_RT_bps , i_h )
EXIT iff    fwd_net_APY < T_out
```

Interpretation: "If I were opening this trade fresh **right now** at the current funding rate and basis, would my entry-style gate approve it at the more lenient exit threshold? If no → close." The two thresholds are directly comparable.

Two additional exit triggers, evaluated alongside:

- **Max-hold**: position age > max-hold hours → exit.
- **Stop-loss**: `unrealized_PnL / spot_entry_notional ≤ stop-loss-pct` → MANDATORY exit (not deferrable on adverse basis).

Voluntary exits (forward profit, max-hold) **defer** for one cycle when the live basis would print a closing cost above the configured exit-basis ceiling. The deferred exit retries every cycle until basis becomes favourable. Stop-loss, hedge-integrity, and market-unhealthy exits are NOT deferrable.

#### Reservation-aware sizing (inside the walk loop)

The matching engine on both venues reserves `qty × limit_price` (not avg-fill price) from the trade wallet when a limit-IOC buy lands. The limit price is `worst_walked_price + tick_buffer`, which is strictly above the average fill. Sizing `target_qty` purely against `sized_notional / mid_price` overflows the wallet's free balance on thin books and causes mid-fill "balance insufficient" errors with partial-fill exposure.

The walk loop converges by clamping `target_qty` every pass:

```
spot_limit  = spot_worst_price × (1 + tick_buffer / 10⁴)
perp_limit  = perp_worst_price × (1 − tick_buffer / 10⁴)

max_qty_by_spot_balance = (spot_leg_free × safety_factor) / spot_limit
max_qty_by_perp_balance = (perp_leg_free × safety_factor × leverage) / perp_limit

target_qty = min(target_qty, spot_book_fillable, perp_book_fillable,
                 max_qty_by_spot_balance, max_qty_by_perp_balance)
```

The safety factor (~0.99) absorbs fee accrual and rounding. Up to 4 passes; walks re-execute when target_qty shrinks so the profitability gate downstream sees fill prices for the FINAL (post-clamp) size, not pre-clamp.

#### Per-strategy config that applies

See [§4 Configuration](#4-configuration) for the layered split. Per-strategy fields used here: entry/exit net-APY thresholds, exit-basis buffer multiplier, exit-basis ceiling, stop-loss percent, max-hold hours, min/max position percent of equity, entry/exit tick-buffer bps, perp leverage, auto-transfer flag, auto-quote-swap flag, futures-buffer percent. Global fields used: max open positions, max trades per day, loop seconds, paper-mode synthetic costs, hedge integrity check, delisting check.

### 3.2 Cross-venue funding arb [PLANNED]

Reserved trade-type identifiers: `binance_kucoin_cross_funding_arb` (spot on one venue, perp on the other), `ibkr_binance_funding_arb`, `ibkr_kucoin_funding_arb` (IBKR equity / option hedge against a CEX perp).

Thesis (when implemented): when two venues have meaningfully different funding rates for the same perp, take the cheap-funding venue as the long-funding side and the expensive-funding venue as the short-funding side. SOP + math TBD; will require an extended wallet model (balances live on different venues) plus venue-vs-venue basis modelling.

### 3.3 Onchain [PLANNED]

Reserved trade-type identifier: `onchain_binance_funding_arb` (DEX perp + CEX spot). Chain / protocol still TBD per operator. Will land as a new venue gateway plus an onchain-specific module for RPC, wallet, and signing concerns.

---

## 4. Configuration

The bot has **three layers** of configuration. Each layer answers a different question.

| Layer | Who edits | Where | Lifetime |
|---|---|---|---|
| **A. Environment variables** | Operator (one-time) | Coolify env vars | Restart of container |
| **B. Compile-time defaults** | Developer | Source code | Code release |
| **C. Runtime config tables** | Operator (via `/config` UI) | DB | Edited live, persists across restarts |

### Why all three?

- **A** holds secrets (API keys, DB URL, dashboard password, diagnostics token). Never in code or DB.
- **B** holds **first-run defaults** used to seed a fresh runtime config when the DB has no rows yet. Once the rows exist, B is no longer consulted. They exist so a fresh deployment has sane thresholds out of the box.
- **C** is the **live source of truth** at runtime. Once the rows exist, every threshold the bot reads comes from C. Edits on `/config` write to C immediately.

This means: **changing the compile-time defaults does NOT change running-bot behavior** unless the runtime row is wiped or the matching field is edited via `/config`. The defaults only set the initial seed.

### A. Environment variables

See §2.2 for the full list. Read once at process start.

### B. Compile-time defaults

Mirror the per-strategy + global runtime fields below, with the same default values. The historic names use the word "FUNDING" where they should say "NET APY" — treat as alias; rename queued.

### C. Runtime config — two tables

| Table | Scope | Rows | Edits via |
|---|---|---|---|
| **Global config** | applies across every active strategy and mode | 1 (singleton) | `/config` form, any tab |
| **Per-strategy config** | one row per strategy (trade-type) | one per active strategy, seeded on first read | `/config?strategy=<strategy>` tab |

The bot, when running on a given strategy, reads a **merged view** of the two tables — strategy-specific fields come from the per-strategy row, everything else from the global row. Writes follow the same routing.

#### Split rationale

| Field category | Per-strategy? | Why |
|---|---|---|
| **Thresholds** (entry/exit min net APY, exit-basis buffer multiplier, max-exit-basis bps, stop-loss percent) | **Yes** | Different strategies, different risk profiles. |
| **Sizing** (min/max position percent, max-hold hours) | **Yes** | Strategies size differently. |
| **Execution** (entry/exit tick buffer bps, perp leverage, max perp leverage) | **Yes** | Venue tick density and leverage policy diverge. |
| **Wallet** (auto-transfer flag, auto-quote-swap flag, futures-buffer percent) | **Yes** | Unified-margin venues don't need these; Classic does. |
| **Account-level caps** (max open positions, max trades per day) | **No** (global) | These cap portfolio risk across the entire account. |
| **Process** (loop seconds) | **No** (global) | One bot process, one loop period. |
| **Mode** (paper starting equity, paper slippage bps, paper fee bps) | **No** (global) | Mode-level; apply to every strategy in that mode. |
| **Safety switches** (delisting check, hedge-integrity check) | **No** (global) | Apply uniformly. |
| **Migration cursor** | **No** (global) | Schema-level. |

#### Active fields

**Per-strategy:**

| Field | Default | Purpose |
|---|---|---|
| Entry minimum net APY | 0.20 (= 20%) | Open only when net APY ≥ this. |
| Exit minimum net APY | 0.05 (= 5%) | Forward net APY below this → close. |
| Exit-basis buffer multiplier | 3.0 | The `m` in the worst-case-exit-basis formula. |
| Max exit basis bps | 5.0 | Defer voluntary exits until live basis ≤ this. |
| Stop-loss percent | −0.02 (= −2%) | Mandatory exit when unrealized PnL / notional ≤ this. |
| Max-hold hours | 72 | Time-based exit. |
| Min position percent | 0.005 (= 0.5%) | Sizing floor as fraction of equity. |
| Max position percent | 0.10 (= 10%) | Sizing ceiling. |
| Entry tick-buffer bps | 1.0 | Limit-IOC entry price padding above worst-walked fill. |
| Exit tick-buffer bps | 2.0 | Same on exit. |
| Perp leverage | 1 | Only safe value for delta-neutral. |
| Max perp leverage | 1 | Hard cap (also shown on dashboard for effective-APY display). |
| Auto-transfer flag | true | Pre-trade spot↔futures rebalance for split-wallet venues. |
| Auto-quote-swap flag | true | Auto-swap USDT↔USDC pre-trade when a quote wallet is starved. |
| Futures-buffer percent | 0.20 (= 20%) | Margin buffer kept on futures wallet during post-cycle drain. |

**Global:**

| Field | Default | Purpose |
|---|---|---|
| Max open positions | 1 | Account-level cap across all strategies / venues, per mode. |
| Max trades per day | 8 | Soft entry cap per mode. |
| Loop seconds | 30 | Cycle period. |
| Paper starting equity | 1000 USDT | Paper virtual capital. |
| Paper slippage bps | 5 | Paper synthetic slippage cost. |
| Paper fee bps | 4 | Paper synthetic fee cost. |
| Hedge integrity check | true | Verify both legs exist on the venue every cycle. |
| Delisting check | true | Force-close on market unhealthy. |
| Migration cursor | (managed) | Tracks one-shot schema migrations. Do not edit. |

Master entry/exit/maintenance toggles per mode (paper vs live) live on a separate state table — not on the strategy config. Per-strategy enable/exit-all toggles live on yet another state table keyed by (mode, strategy).

#### How edits route

- **Form GET** accepts a `strategy` query param. Defaults to the first active strategy. The form shows that strategy's per-strategy values + the (shared) global values.
- **Form POST** carries a hidden `strategy` field set by the active tab. Writes route per-strategy fields to that strategy's row; global fields to the singleton. So editing global fields from any tab updates the values every tab sees; editing per-strategy fields only updates the active tab's row.

#### Schema version cursor

A persisted integer on the global table gates one-shot migrations so they run exactly once per row. Migration history:

- v0 → v1: legacy per-period → APR semantic (one-time numeric rescale).
- v1 → v2: threshold rename from "funding threshold" to "min net APY" — name change only, values copied.
- v2 + per-strategy split: no value migration. Per-strategy rows are lazily seeded from the current global values on first read of each strategy; subsequent edits per strategy diverge.

#### Fields not to bring back (lessons-encoded)

Earlier iterations carried fields that were retired as the strategy matured. A from-scratch rewrite should **NOT** reintroduce these:

- **Hard basis sanity gate** (e.g. "reject if |basis| > X bps"). The profitability gate (funding + signed basis − fees) is the single economic check; a standalone basis gate double-counts and rejects valid positive-basis trades. See §16 Learning L08.
- **Scan-time liquidity heuristics** (24h volume, order-book depth, depth-band bps). The book walk at sizing time is the real liquidity check. See §16 Learning L09.
- **Notional-floor sizing** (absolute USDT). Use percent-of-equity (`min_position_pct`, `max_position_pct`). Notional floors don't scale across deposit sizes.
- **Imbalance-threshold knob for rebalance**. The rebalance trigger should be "candidates exist that need both wallets funded", not a static USDT threshold. See §16 Learning L19.
- **Master entry/exit toggles on the strategy config**. Those belong on the mode-state and per-strategy-state tables, not on the runtime config singleton. Mixing them invited stale-read bugs.
- **Names that promise raw funding** for thresholds that are actually net APY. The names lied for months. See §16 Learning L01.

---

## 5. UI

The bot exposes an HTTP UI: HTML pages, plus JSON / Markdown endpoints. **Single operator user**, HTTP Basic auth (username + password supplied via env vars `DASHBOARD_USER` / `DASHBOARD_PASSWORD`) on every route except `/health` and `/api/diagnostics`. There are no roles, no per-user state. The "view" toggle (paper vs live) is a session cookie, not user account state.

### 5.1 Layout

```
┌──────────────────────────────────────────────────────────┐
│  [logo]  paper▾  Dashboard  Transactions  Logs           │
│                  Monitoring  Configuration  Safety       │
├──────────────────────────────────────────────────────────┤
│  (page content)                                          │
└──────────────────────────────────────────────────────────┘
```

Nav bar is in `base.html`. All routed pages extend `base.html`. The paper/live toggle in the top-left switches the `view` cookie via a POST to `/view/{mode}`; the page reloads showing the data for that mode.

### 5.2 Pages

Six routed HTML pages + JSON / Markdown endpoints. **The "purpose" column is the SSOT — if you're rewriting from scratch, build each page to fulfil its purpose, not to mimic the current implementation.**

| Page | Route | Purpose | Key contents (current) |
|---|---|---|---|
| **Dashboard** | `/dashboard` (and `/` redirects here) | "What's happening right now". The operator's home page. | KPI cards (current equity, net injected capital, total PnL, XIRR, open positions, fees paid, free deployable). Open positions table with expandable per-position detail (spot + perp leg cards). Closed positions table. Worker liveness alert. Stuck-position alert. |
| **Transactions** | `/transactions` | "Every fill, sortable/filterable". | Trade rows (entry + exit legs), grouped by position. Per-row: timestamp, venue, leg (spot/futures), side, qty, price, fee. Filterable by date / symbol / mode. |
| **Logs & Scans** | `/logs` | "What did the bot decide, and why didn't it trade?". | Two tables: `BotEvent` log (INFO/WARN/ERROR) + `RejectedCandidate` scan rejections (symbol, reason, funding APY at scan time). Filterable. The diagnostic alphabet from §9 is rendered here. |
| **Monitoring** | `/monitoring` | "Is each venue's API healthy?". | One card per gateway (Binance, KuCoin, Onchain placeholder). Live probes: balance fetch, funding rates fetch, account-type detection, dust endpoint, capital-flow history. Each card shows OK/error per probe. Used to diagnose permission / IP / connectivity issues without diving into logs. |
| **Configuration** | `/config` | "Set thresholds + per-strategy knobs". | Strategy tab strip (per active trade_type). Form with the active strategy's per-strategy fields + the shared global fields. POST routes per-strategy fields to that tab's row, globals to the singleton. See §4. |
| **Safety & Rules** | `/safety` | "Read-only view of the active guardrails + API-key whitelist IP". | Outbound IP for API-key whitelisting. Per-mode entry/exit/maintenance flags. Active guardrails table (thresholds, leverage mode, hedge integrity, market-status check, loop interval). Mirror of `/config` but display-only with hint text. |

### 5.3 Per-strategy UI rules

After PR #35 (per-strategy config split):

- **`/config`** has a tab strip with one tab per active trade_type. Switching tabs reloads with `?strategy=<trade_type>` and shows that strategy's per-strategy fields. Global fields display the same across all tabs.
- **`/dashboard` position rows** carry `trade_type`. **Should** show "Threshold X% (binance_funding_arb)" inline per row so the operator can see per-position thresholds at a glance. *(Today still shows global threshold. Deferred follow-up.)*
- **`/safety`** **should** mirror `/config`'s strategy tabs. *(Today shows global only. Deferred follow-up.)*
- **`/monitoring`** already has per-strategy enable/exit state via `StrategyState`. Per-strategy config can be inlined into the same card per trade_type. *(Today not done.)*
- **`/logs` rejection rows** carry `mode` + `exchange` but not `trade_type` directly. Filter by venue is the proxy; when per-strategy thresholds diverge between binance and kucoin, the rejection reason text already shows the threshold value used, so this is fine without further work.
- **`/transactions`** rows carry `trade_type`. Adding a column or filter for it is a tiny UX improvement.

### 5.4 Form-handling conventions

- All forms POST to a route on the same path family (e.g. `/config` POSTs to `/config`).
- POSTs return `303 See Other` redirecting back to the GET view, with a query param like `?saved=1` so the GET can render a banner.
- Percent fields are submitted as percentages (e.g. `entry_min_net_apy_pct=20.0`) and divided by 100 in the handler. Underlying schema stays in decimal.
- Toggle fields use `0|1` form values; cast to bool in the handler.
- New form names should be additive (don't break old form posts mid-flight — accept aliases for one release cycle).
- `auth: None = Depends(auth)` is the standard pattern; do not bypass it on any non-`/health`, non-`/api/diagnostics` route.

### 5.5 Action endpoints (POSTs)

Beyond config-save, the UI exposes these mutators:

| Endpoint | Action |
|---|---|
| `POST /view/{mode}` | Switch the session's view cookie between `paper` and `live`. |
| `POST /mode/{mode}/start` / `/stop` | Toggle `ModeState.entry_enabled` (and the maintenance flag's inverse). |
| `POST /mode/{mode}/exit-all-stop` | Set maintenance mode + close everything currently open in that mode. |
| `POST /strategies/{mode}/{trade_type}/start` / `/stop` | Per-strategy entry toggle. |
| `POST /strategies/{mode}/{trade_type}/exit-all-stop` | Per-strategy exit-all. |
| `POST /positions/{position_id}/close` | Manual close of a single open position. |
| `POST /run-once` | Trigger one immediate cycle (for testing — usually the auto-loop suffices). |
| `POST /worker/start` | Start the background loop if it died. |
| `POST /admin/reingest-flows` | Replay capital-flow history ingestion. |

Every action POST has a `confirm()` `onsubmit` handler in the template — JS dialog asks "Are you sure?" before the form submits. This is the only soft safety; there's no second-factor or audit-log step.

### 5.6 JSON / Markdown endpoints

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | Liveness for Coolify / uptime monitors. Returns `{"ok": true}`. |
| `GET /api/diagnostics?token=...&hours=...` | `DIAGNOSTICS_TOKEN` | Structured snapshot. See §8. |
| `GET /monitoring/export.md` | dashboard auth | Markdown dump of monitoring state — for pasting into chat / share. |

### 5.7 Templates

| Template | Routes | Notes |
|---|---|---|
| `base.html` | (all) | Nav bar, view toggle, static-asset versioning. |
| `dashboard.html` | `/dashboard` | KPIs + open positions + closed positions + alerts. |
| `transactions.html` | `/transactions` | Trade table with filters. |
| `logs.html` | `/logs` | BotEvent + RejectedCandidate tables. |
| `monitoring.html` | `/monitoring` | Per-gateway probe cards. |
| `config.html` | `/config` | Strategy tab strip + form. |
| `safety.html` | `/safety` | Read-only guardrails view + IP whitelist helper. |

### 5.8 What to keep, what to throw away (for a rewrite)

If we're rebuilding the UI from scratch, **keep**:

- The page taxonomy (Dashboard / Transactions / Logs / Monitoring / Configuration / Safety) — these names match operator vocabulary.
- The paper/live view toggle as a session cookie — it's the right ergonomic.
- The KPI-cards-then-tables layout on Dashboard.
- The expandable per-position leg detail (it's the most operator-friendly thing in the current UI).
- The `/api/diagnostics` JSON contract (§8) — external monitor + cron rely on it.
- `/health` shape (`{"ok": true}`) — Coolify pings this.

**Reconsider:**

- Form-by-percentage convention (`*_pct` field names) — works but produces ugly Python signatures. A nicer pattern: submit decimal, JS displays as percent.
- Mixing display + edit on `/safety` (it duplicates `/config`). Either delete `/safety` or make it strictly an "active state" snapshot that's auto-derived.
- Per-strategy display gaps (§5.3 above) — fix while you're rewriting.
- The "naked spot" rendering path. Today there's special handling in `dashboard.html` for `status='naked_spot'` (suppress fake perp leg). A from-scratch rewrite could unify this into a generic "position has missing leg" component.

**Throw away:**

- Deprecated form fields still showing on `/safety` (`min_24h_quote_volume`, `min_order_book_depth_usdt`, etc. were purged from `/config` in PR #34 — but check `/safety` and `/monitoring` exports for stragglers).
- Any `<input>` whose `name` is still `entry_funding_threshold_pct` rather than `entry_min_net_apy_pct` — accepted as a back-compat alias for one cycle, drop after the next release.
- The strategy placeholder rows on `/config` for trade-types that aren't wired (cross-venue, IBKR) — keep as "coming soon" notes but don't render disabled form controls.

### 5.9 UI dependencies

- **Jinja2** for templating. No SSR framework beyond that.
- **Vanilla JS** for table sorting / filtering / detail-row toggling. Single-file convention; no bundler.
- **CSS** in `app/static/style.css`. Custom theme; no UI framework (no Bootstrap, no Tailwind).
- **No build step.** The `NIXPACKS_NODE_VERSION` env var exists in Coolify but the repo has no JS build — Nixpacks runs a Node phase that does nothing.

If we rewrite, the build-step-free Vanilla setup is worth preserving — it keeps the deploy story trivial. A from-scratch UI in HTMX + a tiny amount of Alpine.js (or pure server-rendered Jinja, same as today) would be appropriate. Avoid React / SPA churn.

---

## 6. Wallet model per venue

### 6.1 Binance Portfolio Margin (active)
- Unified pool: one balance per asset, used for both spot and perp.
- Synthesised `futures.<asset>.free` mirrors `spot.<asset>.free`; `futures.<asset>.total = 0` by convention to avoid double-counting in equity sums.
- Account-mode probe returns "unified".
- `transfer_*_to_spot` / `_to_futures` are no-ops on PM (return early).
- Balance fetch: `/papi/v1/balance` → free = `crossMarginFree + umWalletBalance + cmWalletBalance`.

### 6.2 KuCoin Classic (active)
- Three+ spot wallets: `main` (deposits land here), `trade` (where spot orders execute), `margin`, `isolated`, plus `contract` (futures margin) and `pool` (Earn, time-locked).
- Account mode probe declares this NOT unified.
- Spot orders execute against `trade` only. The abstraction MUST sweep all sibling spot wallets (`main`, `margin`, `isolated`) into `trade` at the start of every cycle so the synthesised "spot free balance" matches what the order book can actually spend. **Do not** present an aggregated balance figure derived from multiple wallets without first physically consolidating them.
- Futures balance is **per-currency** on the venue — fetch USDT and USDC separately. A default fetch returns USDT only.
- **Transfer routing — futures→spot is a TWO-HOP.**
  - **Step 1** (Futures → Main): use the **futures-side** legacy transfer-out endpoint. The spot-side universal-transfer endpoint cannot drain the futures wallet and returns "balance insufficient" even when the futures available-balance is positive.
  - **Step 2** (Main → Trade): use the spot-side inner-transfer endpoint. The futures-side transfer-out ignores any "deposit into trade" hint and always lands funds in Main; an inline hop is required so the drain returns "funds are spendable on the spot leg right now". Without the hop, funds wait a cycle for the next consolidation sweep — and that's the window the original drain↔rebalance oscillation exploited.
  - **Spot → Futures** (the IN direction) uses universal-transfer; that path is fine.
- **Idle-cycle oscillation gating**: pre-trade rebalance MUST only run when at least one candidate passed the scan. Without that gate, every idle cycle reshuffles the same dollars through contract → main → trade → contract and produces a transfer-failure log storm.
- Identical-error dedup: when the same transfer call fails repeatedly with the same message, throttle the log emission. Pattern applies equally to close-error retries.
- Earn-locked balances (Pool) are time-locked; never swept.

### 6.3 KuCoin UTA (not active today)
- Single unified pool. Account mode probe declares unified. Transfer methods are no-ops.

### 6.4 Cross-stable USDT ↔ USDC
- Per-quote sizing reads the spot leg from the spot-quote wallet and the perp leg from the perp-quote wallet — these are independent buckets for cross-stable arbs.
- Auto-swap fires only for same-stable arbs (spot and perp share quote) when the relevant pool is below the min-notional and the other stable has surplus.
- Swap path: walk the USDC/USDT order book, place a limit-IOC at the worst-walked-price ± tick buffer, with a ±50 bps de-peg guard. Cost is charged to the profitability gate.

---

## 7. Database schema

SQLite by default (configurable via `DATABASE_URL`). Schema is defined declaratively. Migrations are additive-only — new columns added at startup, never dropped, always backwards-compatible.

### 7.1 Tables

**This schema is a frozen contract.** The rewrite must preserve every table + column listed below by name and semantic. Adding new columns is fine (additive policy). Renaming or dropping anything breaks the doc-update history, the in-flight migration cursor, and any external tooling reading the SQLite file directly.

| Table | Purpose |
|---|---|
| `strategy_config` | Singleton, global runtime config (account/process/mode-level fields). See §4. Row id always `1`. |
| `strategy_config_per_strategy` | One row per strategy (`trade_type`). Holds strategy-specific runtime config. Unique key on `trade_type`. |
| `mode_state` | Per-mode (paper/live) master toggles: `entry_enabled`, `exit_enabled`, `maintenance_mode`. PK: `mode`. |
| `strategy_state` | Per-(mode, trade_type) strategy-level toggles: `entry_enabled`, `exit_all_pending`. PK: composite (`mode`, `trade_type`). |
| `positions` | Lifecycle: `open → naked_spot → closed`. Carries entry prices, funding accruals, last close error. |
| `trades` | One row per fill. Tagged to a `position_id` (nullable). Used by P&L + transactions UI. |
| `bot_events` | Logs at INFO / WARN / ERROR. Source of the `/logs` UI and `recent_events` in `/api/diagnostics`. Timestamp column is `ts`. |
| `rejected_candidates` | Scan rejections — symbol, reason, funding APR at scan time, ts. Source of the `/logs` UI and `rejections_grouped` in `/api/diagnostics`. |
| `balance_snapshots` | Per-cycle wallet snapshot per (mode, venue). Source of trailing equity. |
| `equity_curve` | Per-cycle equity history per (mode, venue). |
| `capital_flows` | Deposits / withdrawals / sub-transfers, ingested from venue history. Source of net-injected-capital figure + XIRR. Unique key on `external_id` for idempotent ingestion. |
| `scan_results` | Per-cycle scan summary (action, candidate counts, top candidates). |

### 7.1.1 Frozen column contracts

Only the columns the rewrite MUST preserve are listed here. Additional columns may have accumulated over time (see additive policy in §7.3); the rewrite can carry them as-is or ignore them, but should not drop them.

**`strategy_config`** (singleton with `id = 1`):

| Column | Type | Semantics |
|---|---|---|
| `id` | integer PK | Always `1`. |
| `config_schema_version` | integer, default 0 | Persisted migration cursor. Gates one-shot value transforms. |
| `max_open_positions` | integer, default 1 | Account-level cap across all strategies / venues per mode. |
| `max_trades_per_day` | integer, default 8 | Soft per-mode entry cap (UTC day). |
| `loop_seconds` | integer, default 30 | Cycle period. |
| `paper_starting_equity` | float, default 1000 | Paper virtual capital. |
| `paper_slippage_bps` | float, default 5 | Paper synthetic slippage. |
| `paper_fee_bps` | float, default 4 | Paper synthetic fee. |
| `enforce_hedge_check` | bool, default true | Phase-A hedge integrity verification toggle. |
| `delisting_check` | bool, default true | Phase-A market-health verification toggle. |
| `updated_at` | datetime | Auto-updated. |

Legacy columns retained for back-compat (no longer read): `entry_funding_threshold`, `exit_funding_threshold` (renamed → `entry_min_net_apy`, `exit_min_net_apy` on the per-strategy table); deprecated columns enumerated in §4.

**`strategy_config_per_strategy`** (one row per active `trade_type`):

| Column | Type | Semantics |
|---|---|---|
| `id` | integer PK | Auto-increment. |
| `trade_type` | string, unique | Strategy identifier (e.g. `binance_same_venue_funding_arb`). |
| `entry_min_net_apy` | float, default 0.20 | Open only when net APY ≥ this. |
| `exit_min_net_apy` | float, default 0.05 | Forward net APY below this → close. |
| `exit_basis_buffer_multiple` | float, default 3.0 | Worst-case-exit-basis multiplier (`m` in §3.1 math). |
| `max_exit_basis_bps` | float, default 5.0 | Defer voluntary exits until live basis ≤ this. |
| `stop_loss_pct` | float, default −0.02 | Mandatory exit when `unrealized_PnL / notional ≤ this`. |
| `max_hold_hours` | integer, default 72 | Time-based exit. |
| `min_position_pct` | float, default 0.005 | Sizing floor as fraction of equity. |
| `max_position_pct` | float, default 0.10 | Sizing ceiling. |
| `entry_tick_buffer_bps` | float, default 1.0 | Limit-IOC entry price padding above worst-fill. |
| `exit_tick_buffer_bps` | float, default 2.0 | Same on exit. |
| `perp_leverage` | integer, default 1 | Only safe value for delta-neutral. |
| `max_perp_leverage` | integer, default 1 | Hard cap (also displayed on dashboard). |
| `auto_transfer_enabled` | bool, default true | Pre-trade spot↔futures rebalance toggle. |
| `auto_quote_swap_enabled` | bool, default true | Auto-swap USDT↔USDC pre-trade toggle. |
| `futures_buffer_pct` | float, default 0.20 | Margin buffer kept on futures wallet during post-cycle drain. |
| `updated_at` | datetime | Auto-updated. |

**`positions`**:

| Column | Type | Semantics |
|---|---|---|
| `id` | integer PK | Auto-increment. |
| `mode` | string, indexed | `'paper'` or `'live'`. |
| `exchange` | string, indexed | Venue id (`binance`, `kucoin`). |
| `trade_type` | string, indexed | Strategy identifier. |
| `status` | string, indexed | `'open'` \| `'naked_spot'` \| `'closed'`. Open-statuses tuple: `('open', 'naked_spot')`. |
| `symbol` | string, indexed | Base asset (`BTC`, `ETH`). |
| `spot_symbol` | string | e.g. `BTC/USDT`. |
| `perp_symbol` | string | e.g. `BTC/USDT:USDT`. |
| `quote_currency` | string, indexed | Perp's quote (e.g. `USDT`). |
| `spot_quote_currency` | string, indexed | Spot's quote (differs from `quote_currency` for cross-stable arbs). |
| `quantity` | float | Base-asset qty. |
| `opened_at` | datetime | Set at first leg fill. |
| `entry_funding_rate` | float | At entry (decimal per window). |
| `last_funding_rate` | float | Refreshed every cycle while open. |
| `spot_entry_price` | float | Real for `open`; current-mid estimate for reconstructed `naked_spot`. |
| `perp_entry_price` | float | Real for `open`; `0` sentinel for `naked_spot` (UI must suppress display). |
| `funding_interval_hours` | float, default 8 | Read from contract metadata. |
| `funding_income_accrued` | float, default 0 | Paper-mode accrual; live mode infers from balance delta. |
| `last_funding_accrual_ts` | datetime | Last accrual touch. |
| `last_close_error` | text, default empty | Stuck-close diagnostic; empty when none. |
| `closed_at` | datetime, nullable | Set when status → `'closed'`. |

**`trades`**:

| Column | Type | Semantics |
|---|---|---|
| `id` | integer PK | |
| `mode` | string, indexed | |
| `exchange` | string, indexed | |
| `trade_type` | string, indexed | Mirrored from the parent position. |
| `position_id` | integer, indexed, nullable | NULL for ghost-entries reconstructed during naked-spot recovery. |
| `symbol` | string, indexed | |
| `venue` | string | `'spot'` or `'futures'` — the leg. |
| `side` | string | `'buy'` or `'sell'`. |
| `quantity` | float | |
| `price` | float | |
| `fee` | float, default 0 | |
| `ts` | datetime, indexed | |

**`bot_events`**:

| Column | Type | Semantics |
|---|---|---|
| `id` | integer PK | |
| `mode` | string, indexed | |
| `exchange` | string, indexed | `'binance'`, `'kucoin'`, or `'system'` for cross-venue events. |
| `level` | string | `'INFO'` \| `'WARN'` \| `'ERROR'`. |
| `message` | text | |
| `ts` | datetime, indexed | |
| `requires_action` | bool, default false | Flag for the UI to highlight. |

**`rejected_candidates`**:

| Column | Type | Semantics |
|---|---|---|
| `id` | integer PK | |
| `mode` | string, indexed | |
| `exchange` | string, indexed | |
| `symbol` | string, indexed | Usually the perp symbol. |
| `reason` | text | Free-form: `<category> (<detail>)`. The category before the parenthesis drives `rejections_grouped` in diagnostics. |
| `funding_rate` | float | APR at scan time (for sorting / display). |
| `ts` | datetime | |

**`mode_state`**:

| Column | Type | Semantics |
|---|---|---|
| `mode` | string PK | `'paper'` or `'live'`. |
| `entry_enabled` | bool, default true | Master entry switch for this mode. |
| `exit_enabled` | bool, default true | Master exit switch. |
| `maintenance_mode` | bool, default false | When true: block new entries, force-close existing positions. |
| `updated_at` | datetime | |

**`strategy_state`**:

| Column | Type | Semantics |
|---|---|---|
| `mode` | string, PK part | |
| `trade_type` | string, PK part | |
| `entry_enabled` | bool, default true | Per-strategy entry toggle. |
| `exit_all_pending` | bool, default false | When true: force-close every open position of this strategy + disable entries. |

**`balance_snapshots`**, **`equity_curve`**, **`capital_flows`**, **`scan_results`**: structurally simpler tables; preserve `ts`, `mode`, `exchange`, and the obvious value columns. `capital_flows.external_id` must remain `UNIQUE` so re-ingestion of venue history is idempotent.

### 7.2 Position lifecycle

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

**Stale reconciliation**: at the top of every live cycle, any naked-spot position whose underlying spot balance is no longer in the wallet (sold externally, dust-converted by a prior cycle, Earn redemption, etc.) is auto-closed.

### 7.3 Migration policy

- **Additive only.** Never drop columns. Code may stop reading a column; the column stays.
- New columns get a sensible `DEFAULT`. Idempotent.
- One-shot value transforms gated by `config_schema_version` so they run exactly once per row regardless of restarts.

---

## 8. Monitoring & diagnostics

### 8.1 `/api/diagnostics?token=<DIAGNOSTICS_TOKEN>&hours=<1-168>`

Auth: `?token=...` matched against the `DIAGNOSTICS_TOKEN` env var. Returns `503` if the env var is unset (refuses to be silently public).

**This shape is a frozen contract.** The external diagnostics cron, the tracker-issue post script, and any monitor chat that subscribes to the tracker all rely on it. The rewrite MUST preserve it byte-for-byte; any field added is fine, any field removed or renamed breaks downstream.

Response shape (all timestamps are ISO-8601 UTC strings ending in `Z`):

```
{
  "generated_at_utc":   string,    // when this snapshot was produced
  "window_hours":       integer,   // lookback applied to event / trade / rejection counts (caller's ?hours= clamped to [1,168])

  "cycle_health": {
    "last_event_ts":            string | null,   // ISO-8601 UTC or null if no events ever
    "last_event_msg":           string | null,   // truncated to 240 chars
    "seconds_since_last_event": number | null,   // float seconds, or null
    "error_count":              integer,         // count of ERROR-level events in window
    "warn_count":               integer          // count of WARN-level events in window
  },

  "positions": {
    "by_status": { string: integer },            // e.g. {"open": 2, "naked_spot": 1, "closed": 14}
    "open":   [ OpenPosition ],                  // every Position with status == "open"
    "naked":  [ NakedPosition ]                  // every Position with status == "naked_spot"
  },

  "wallets": {
    venue_id: {                                  // e.g. "binance", "kucoin"
      asset: {                                   // e.g. "USDT", "USDC"
        wallet_type: {                           // e.g. "main", "trade", "contract", "margin", "isolated", "pool"
          "free":  number,
          "total": number
        }
        OR
        wallet_type: { "error": string }         // when the per-wallet probe failed
      }
    }
  },

  "rejections_grouped": {
    "<venue>/<mode>": {                          // e.g. "binance/live": { ... }
      reason_category: integer                   // count per Tier-1/2/3 reject reason
    }
  },
  "rejections_total":      integer,              // sum across all (venue, mode, reason)

  "recent_events": [                             // ≤ 50, newest first, WARN + ERROR only
    {
      "ts":       string,                        // ISO-8601 UTC
      "level":    "WARN" | "ERROR",
      "exchange": string,                        // e.g. "binance", "kucoin", "system"
      "mode":     "paper" | "live",
      "msg":      string                         // truncated to 400 chars
    }
  ],

  "recent_trades": [                             // ≤ 50, newest first
    {
      "ts":        string,
      "mode":      "paper" | "live",
      "exchange":  string,
      "symbol":    string,                       // venue-shape symbol (e.g. "BTC/USDT" for spot, "BTC/USDT:USDT" for perp)
      "venue_leg": "spot" | "futures",
      "side":      "buy" | "sell",
      "qty":       number,
      "price":     number,
      "fee":       number
    }
  ],
  "recent_trades_count":  integer,               // length of recent_trades

  "anomalies": [
    {
      "severity": "critical" | "warn" | "info",
      "rule":     string,                        // anomaly rule identifier from §8.2
      "detail":   string                         // human-readable explanation
    }
  ],
  "anomalies_count":      integer                // length of anomalies
}

OpenPosition: {
  "id":                  integer,
  "mode":                "paper" | "live",
  "exchange":            string,
  "symbol":              string,                 // base asset (e.g. "BTC")
  "perp_symbol":         string,
  "quote_currency":      string,                 // perp's quote (e.g. "USDT")
  "quantity":            number,                 // in base units
  "spot_entry_price":    number,
  "perp_entry_price":    number,
  "last_funding_rate":   number,                 // decimal (e.g. 0.001 = 0.1% per window)
  "funding_interval_h":  number,                 // hours per funding window (4 or 8)
  "last_close_error":    string,                 // empty string if no close error
  "age_hours":           number
}

NakedPosition: {
  "id":                  integer,
  "mode":                "paper" | "live",
  "exchange":            string,
  "symbol":              string,                 // base asset
  "spot_symbol":         string,
  "quantity":            number,
  "spot_entry_price":    number,                 // current-mid estimate when reconstructed
  "notional_est":        number,                 // quantity × spot_entry_price
  "age_minutes":         number
}
```

Constraints / invariants the rewrite must preserve:

- Endpoint path is exactly `/api/diagnostics`.
- Authentication is exactly `?token=<value>` matched against the `DIAGNOSTICS_TOKEN` env var. No 401 leaks. No alternative auth (no header, no cookie).
- `503` when the env var is unset (with a body that says so).
- `401` when the env var is set but the token is wrong/missing.
- Response is JSON-serializable in one pass; no streaming.
- `generated_at_utc` is freshly produced per request, not cached.
- Anomaly detection runs on every request (it's a derived quantity, not stored).
- Default `hours=24`, max `hours=168`, min `hours=1`. Clamping rather than rejecting.

### 8.2 Anomaly rules

| Rule | Severity | Trigger |
|---|---|---|
| `no_recent_events` | critical | No `BotEvent` in last 3600s |
| `stale_naked_spot` | warn | A `naked_spot` Position older than 60min |
| `no_trades_despite_scans` | warn | 0 recent trades AND > 20 rejections in window |
| `error_burst` | warn | > 20 ERROR events in window |
| `close_blocked` | warn | Open Position with non-empty `last_close_error` |

### 8.3 Cron + tracker

`.github/workflows/diagnostics.yml` runs every 3h. Required repo secrets: `BOT_URL`, `DIAGNOSTICS_TOKEN`.

The cron pipes the JSON into `.github/scripts/diagnostics_post.py`, which uses the **heartbeat model** (PR #27):

- Locates (or creates) a persistent issue titled `[bot-diagnostics] Tracker`.
- Updates its body to the latest full state.
- Reopens it if anyone closed it.
- **Posts a one-line comment EVERY run** (✅ all-clear or ⚠️ N anomalies + top-3).

The comment fires the GitHub webhook every run — that's how the monitor chat hears about all cycles, not just bad ones.

### 8.4 Monitor chat

Separate Claude session. Reads this doc on every wake-up. Subscribes to the tracker via `subscribe_pr_activity`. Responds per the policy in §11.

---

## 9. Logs & rejection categories

### 9.1 Rejection categories (`rejections_grouped`)

| Category | Meaning | Action when dominant |
|---|---|---|
| `below_threshold` | Tier-1 pre-filter: approx net APY < entry threshold. | None — strategy designed to skip these. |
| `no_spot_market` | Perp's base has no spot pair on the venue. | None — perp-only listing. |
| `insufficient_annualized_profit` | Tier-3 gate: real net APY (after live fees + fill basis) < threshold. | None unless threshold mis-calibrated. |
| `below min position pct` | Sized notional < `min_position_pct × equity`. Wallet starvation. | Inspect `wallets` for stranded funds. |
| `below_min_pct_after_clamp` | Reservation clamp shrunk size below min. | None — genuinely too small. |
| `no_book_depth` | The book-walk simulation returned zero fill; the underlying error is embedded in the rejection reason. | Inspect the inner error (venue limit param, bad symbol, network). |
| `reservation_clamp_zeroed` | Wallet too small even for limit-price reservation. | None. |
| `basis_dislocated` | **DEPRECATED** — gate retired in PR #9. Should be 0. If non-zero, regression. |
| `spot_buy_error: ... Balance insufficient!` | Mid-fill reservation overflow (pre-PR-#10 era) or thin-book partial fill. | After PR #10/14/15 should drop near zero. |
| `spot_buy_error: ... Order size below minimum` | Sizing dropped below venue min after clamp. | Investigate sizing math. |
| `spot_ioc_zero_fill` / `perp_ioc_zero_fill` | Book moved during round-trip. Transient. | None — retries next cycle. |
| `strategy_disabled:<trade_type>` | Operator killed strategy via `/config`. | None unless unintentional. |

### 9.2 Common log patterns (informational)

- `Spot wallet consolidate <asset>: X main→trade` — KuCoin Classic sweep working.
- `Wallet snapshot <q> [Classic|UTA]·split|unified: spot free/total=...; fut free/total=...` — per-cycle wallet state.
- `Pre-trade rebalance skipped: <venue> reports unified margin` — PM/UTA correctly detected.
- `Pre-trade rebalance: X USDT spot→futures (equalize wallets so both legs can fund)` — Classic rebalance working.
- `Scan top <symbol>: predicted rate=X% per Yh → APY=Z%` — top-3 candidate diagnostic per cycle.
- `Phantom spot RESCUED into a hedged position` — phantom-recovery hedge succeeded; orphan spot is now a real hedged position.
- `Phantom spot CLOSED: sold ... → USDT` — phantom-recovery sell-back succeeded; orphan flattened.
- `Phantom dust detected: ... below venue min` — too small to sell, flagged for dust sweep.
- `Dust sweep CLOSED N naked_spot position(s)` — auto-conversion to BNB/KCS succeeded.
- `Stale naked_spot reconciled: <asset> no longer in spot wallet — marked closed` — stale cleanup fired.

### 9.3 Log patterns that indicate a regression

- `Loop iteration error (<mode>): name '<X>' is not defined` — Python NameError from a missing import. Open a PR. Past examples: `total_funding_income`, `rt_basis_bps`.
- `Reservation clamp on <symbol>` — should NOT appear (clamp moved inside walk loop in PR #12). If it surfaces, regression.
- `basis_dislocated` rejections — should be 0. Regression if non-zero.

---

## 10. Failure modes & recovery

| Failure | Detection | Recovery |
|---|---|---|
| Partial fill under `spot_buy_error` | Pre/post-balance snapshot delta in entry path | Synthesize partial fill, continue to perp leg at smaller qty (PR #14) |
| Naked spot left behind | Phantom-recovery sweep every live cycle (Phase A) | Try to hedge with matching perp if profitable, else sell back, else flag as dust |
| Dust below MIN_NOTIONAL | Notional check in recovery | Convert to venue's native fee token (BNB / KCS) via the venue's dust-conversion endpoint |
| Naked spot whose underlying spot disappeared from wallet | Stale-reconciliation pass at top of recovery | Auto-mark closed |
| Wallet starvation | "below min position pct" rejection | Wallet-breakdown diagnostic surfaces where funds are stranded (`/api/diagnostics` payload includes the per-wallet-type view) |
| Book moves during round-trip | Zero-fill on the limit-IOC | Reject, retry next cycle |
| KuCoin futures→spot drain "balance insufficient" / wallet oscillation | Post-cycle drain WARN repeating every cycle on idle accounts (21k+/24h) | **Resolved across PR #29 + PR #33.** Three layers: (1) routing — switched from spot-side universal-transfer to the futures-side legacy transfer-out endpoint, which is the only one that can see the futures wallet; (2) two-hop — append the main→trade hop via spot inner-transfer so funds land where the spot order book can spend them; (3) idle-cycle gate — pre-trade rebalance only fires when a candidate passed the scan, breaking the drain↔rebalance oscillation. Persistent failures are deduped via an error-message throttle. |
| Symbol drift across ccxt versions | Exit funding miss WARN | Falls back to stale `last_funding_rate`; logged. |
| Loop crash | Outer exception handler at the cycle boundary | Logged as ERROR with the full traceback; loop continues next cycle. (Crashes that silently swallow whole cycles are the most insidious failure mode — see §16 L02.) |

---

## 11. Response policy (monitor chat)

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
| New venue error code not handled | **Open PR** adding handler + new rejection category in §9.1. |
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
5. **Update SYSTEM.md** in the same PR if behavior changed (§13 makes this binding).

---

## 12. Crons

| Job | Schedule | Trigger | Side effects |
|---|---|---|---|
| Bot's own loop | every `cfg.loop_seconds` (default 30s) | in-process thread per (mode, gateway) | runs the full cycle in §3.1 |
| Diagnostics workflow | `0 */3 * * *` (every 3h) | GitHub Actions cron | hits `/api/diagnostics`, updates the persistent tracker issue body, posts heartbeat comment. Comment fires the webhook to the monitor chat. |

No external crons beyond these.

---

## 13. Doc-update policy

**Binding.** Every PR that changes BEHAVIOR — not just refactors — must update `docs/SYSTEM.md` in the same PR. Specifically:

- New strategy or trade-type → §3.
- New phase / step in the cycle → §3.1 SOP.
- Math change (formula, threshold default, gate logic) → §3.1 math + §0 definitions if a new term is used.
- New venue / wallet type / transfer route → §6.
- New DB column or status value → §7.2.
- New env var → §2.2 + §4.
- New runtime config field (global or per-strategy) → §4.
- New `/api/*` endpoint or anomaly rule → §8.
- New rejection category or log pattern → §9.
- New failure mode + recovery → §10.

Reviewers reject PRs that change behavior without updating this doc. When in doubt, add a one-liner — better to over-document than under.

---

## 14. Known fragile / deferred

- **Vultr Auto Backups are NOT enabled.** Single-instance SQLite DB on local NVMe. Loss = entire trade / position / event history. Enable Vultr backups (~$1/mo) or run an off-host backup cron.
- ~~**Per-strategy config split**~~ — shipped in PR #35. New `StrategyConfigPerStrategy` table; `MergedConfig` proxy reads global + per-strategy.
- ~~**Naming**: `entry_funding_threshold` / `exit_funding_threshold` are misleading~~ — renamed in PR #32 to `entry_min_net_apy` / `exit_min_net_apy`. Legacy form names still accepted as aliases for one release cycle.
- **Deprecated config fields** (§4) are still on the `/config` form for back-compat. Tidy-up PR pending.
- **Maker-on-exit fee optimization** not implemented. ~30% of exit fees could be saved with post-only-with-timeout-fallback.
- **Symbol mapping drift** across ccxt versions could leave open positions un-lookupable for exit funding refresh. Currently logs a WARN and falls back to stale `last_funding_rate`.
- **Cross-venue + onchain strategies** are roadmap, not implemented.
- ~~KuCoin `futures→spot` drain 112002 / 250001 / oscillation~~ **resolved in PR #29 (routing) + PR #33 (two-hop + idle-cycle gate)** — see §6.2.

---

## 15. Changelog

Append-only. Format: `YYYY-MM-DD · PR# · §sections touched · summary`.

| Date | PR | Sections | Summary |
|---|---|---|---|
| 2026-05-11 | #35 | §4, §14 | Per-strategy config split: new `StrategyConfigPerStrategy` table (one row per trade_type) holds strategy-specific fields (thresholds, sizing, execution, wallet). `StrategyConfig` keeps account/process/mode-level globals. `MergedConfig` proxy lets bot.py call sites read transparently — pass `trade_type` to `get_strategy_config()`. `/config` gets a strategy tab selector; POSTs route per-strategy fields to the active tab's row, global fields to the singleton. Per-strategy rows lazily seeded from global on first read. |
| 2026-05-11 | #34 | §0, §3.1 math, §4, §14 | Renamed `entry/exit_funding_threshold` → `entry/exit_min_net_apy` (config_schema_version v1→v2 migration). Removed deprecated form fields (`max_entry_basis_bps`, `min_24h_quote_volume`, `min_order_book_depth_usdt`, `depth_band_bps`). Form accepts both new and legacy field names for one release cycle. |
| 2026-05-11 | #33 | §3.1, §6.2, §10, §14 | Break KuCoin drain↔rebalance oscillation. (1) Gate pre-trade rebalance on `candidates_passing > 0` (no point equalising wallets when there's no trade to fund). (2) `transfer_futures_to_spot` is now a two-hop: futures `CONTRACT → MAIN` via `transferOut`, then spot `MAIN → TRADE` via inner-transfer. Funds land where the spot order book can spend them without waiting a cycle for `consolidate_spot_wallets`. |
| 2026-05-11 | #32 | tooling | `diagnostics_post.py`: post heartbeat comment **before** body edit, make body edit non-fatal, trim payload. Body-too-large 504s no longer block the comment, which is what fires the monitor chat webhook. |
| 2026-05-11 | #31 | rewrite | SYSTEM.md v1.0 — full rewrite after operator audit. Definitions upfront, per-strategy SOP + math, config layers explained, deprecated fields called out, exit-logic regression `rt_basis_bps` → `rt_basis_signed_bps` fixed alongside. |
| 2026-05-11 | #29 | §6.2, §10, §14 | KuCoin futures→spot drain uses the futures-side `transferOut` endpoint (legacy `/api/v1/transfer-out`) instead of the spot-side universal-transfer (which can't see the futures wallet). `wallet_breakdown` USDC contract under-report fixed. Identical-error dedup via `_TRANSFER_ERROR_CACHE`. Resolved the 112002 deferred item. |
| 2026-05-11 | #30 | §7.2, §10 | Don't render fake perp leg for `naked_spot`; auto-close stale naked rows. |
| 2026-05-11 | #27 | §8, §11, §12 | Heartbeat-model diagnostics tracker. Monitor always knows the cron ran. |
| 2026-05-11 | #26 | §2 | Vultr specs + KuCoin permissions clarification + backup-risk callout. |
| 2026-05-11 | #25 | §2, §4 | Operator-provided setup details rolled in. |
| 2026-05-11 | #24 | new | SYSTEM.md v0.1 first cut. |
| 2026-05-11 | #21 | §3.1, §10 | Auto-convert dust to BNB/KCS via venue dust endpoints. |
| 2026-05-11 | #20 | §9.3, §10 | Fix `total_funding_income` NameError. Silence dust spam. LDUSDT filter. Workflow label fallback. |
| 2026-05-11 | #18 | §8 | `/api/diagnostics` endpoint + GitHub Actions cron + tracker. |
| 2026-05-11 | #17 | §7.2, §9 | Naked positions are first-class in dashboard + transactions. |
| 2026-05-11 | #16 | §3.1 math | Charge auto-swap fees in profitability gate. |
| 2026-05-11 | #15 | §6.2, §10 | Hedge phantom spot via perp when profitable. KuCoin futures per-currency fetch. |
| 2026-05-11 | #14 | §3.1 SOP, §10 | Recover orphaned spot positions + partial-fill detection. |
| 2026-05-11 | #13 | §6.2 | KuCoin sweep margin/isolated + `wallet_breakdown` diagnostic. |
| 2026-05-11 | #12 | §3.1, §6, §7 | Audit cleanups: reservation clamp in walk loop, exit funding refresh, sign math, migration v1, dead-config purge. |
| 2026-05-11 | #11 | §9 | `below_threshold` log shows net APY (the number actually compared). |
| 2026-05-10 | #10 | §3.1 math | Reservation-aware target_qty clamp. |
| 2026-05-10 | #9 | §3.1, §4 | Dropped `basis_dislocated` gate; profitability-only economic check. |
| 2026-05-10 | #8 | §3.1, §9 | KuCoin book-walk limit fix, sign-aware basis, funding APY diagnostic. |
| 2026-05-10 | #7 | §6.2 | KuCoin Classic spot-wallet consolidation. |

(Older history in `git log`.)

---

## 16. Learnings

The hard lessons distilled from the first implementation. **Read this section before writing a single line of the rewrite.** Each item below cost real hours / dollars / log spam to discover. The doc-update-policy in §13 makes additions to this list binding too.

### Naming + correctness

- **L01 — Names that lie cost more than ugly names.** A threshold called `entry_funding_threshold` that actually compares NET APY (after fees + worst-case basis) misled the operator for months. Every threshold name should describe what it's compared AGAINST. The rename to `entry_min_net_apy` should have happened on day one.
- **L02 — Outer try/except + log-and-continue is a silent killer.** Wrapping the cycle loop in `try / except: log_event(ERROR); continue` is correct policy. But a single NameError (a missing import, a renamed variable) caused **every** paper-mode cycle to die for ~15 hours and burn CPU on exception handling, with the only signal being log volume. Two such regressions in this session (`total_funding_income`, `rt_basis_bps`). Mitigation: cycle health monitor on error/cycle ratio, NOT just liveness.
- **L03 — Display ≠ state.** Persisting a `naked_spot` position with `perp_entry_price=0` as a placeholder is fine as long as the UI knows it's a placeholder. Rendering it as a real "−$2.93 MTM" in the perp leg card produced fabricated numbers the operator (correctly) called out. Display layer must know which fields are real vs sentinel.

### Money math

- **L04 — Signed economics, not absolute values.** Treating the entry basis as a cost in `abs()` form (matching the magnitude regardless of sign) is wrong: long-spot/short-perp benefits from positive basis on entry. The bot rejected its own bread-and-butter trades for months. Rule: always derive economic quantities from signed primitives; only take `abs()` when computing magnitudes for display.
- **L05 — Worst-case is conservative AND obvious.** The conservative round-trip basis cost is `−buffer × |entry|` regardless of sign, because the worst-case adverse exit is always "basis moves further positive". A formula that DIFFERS between signs (e.g. `−buffer × |entry|` for positive vs `+buffer × |entry|` for negative) is a sign-bug masquerading as conservatism. Code in this form has been wrong every time we've shipped it.
- **L06 — Funding interval matters more than funding rate.** APY = (1+r)^N where N depends on the funding interval. A "tiny" 0.62% rate at 4h compounds to 510,000% APY. The same 0.62% at 8h compounds to ~715%. Misreading the interval reads as a 700× error in APY.
- **L07 — Reservation ≠ avg-fill cost.** Limit-IOC orders reserve `qty × limit_price`, NOT `qty × mid_price`. Sizing `target_qty = sized_notional / mid` overflows the reservation on thin books → mid-fill "balance insufficient" with real partial-fill exposure. Always size against the limit price the order will actually carry.
- **L08 — One economic gate, not two.** A standalone basis sanity gate that runs BEFORE the profitability gate double-counts (the profitability gate already incorporates basis as a signed input). Single source of economic truth.
- **L09 — Heuristic liquidity gates lie; book walks don't.** Hardcoded thresholds like "min 24h volume", "min order-book depth at ±10 bps band" are crude approximations of "can my trade actually execute?". The real check is to walk the actual book at the actual sizing the bot will use. Heuristics rejected real opportunities AND passed real impossibilities.
- **L10 — Tier-1/2/3 separation pays for itself.** The funding scan can scan thousands of pairs cheaply; the book walk per candidate is expensive; the profitability gate after the walk is the real check. Combining tiers (e.g. always running the full check) burns API rate-limit. Splitting them with progressively-more-expensive checks is the right shape.

### Venue API patterns

- **L11 — Each venue lies differently; document every quirk inline.** KuCoin's book-depth API only accepts `limit=20` or `limit=100` (sliently rejects other values). Binance Futures balance is per-currency (calling without a currency returns USDT only). KuCoin Classic has 3+ spot wallets, only one of which the order book can spend. KuCoin's futures→spot drain needs the futures-side `transferOut`, NOT the spot-side universal-transfer. These aren't documented anywhere except this doc + the code that exercised them. **Every new venue quirk discovered in production goes here, immediately.**
- **L12 — Partial fills under error responses are real.** Some venues' "balance insufficient" responses come AFTER a partial fill has already occurred (matching engine matches what it can, then trips on remainder reservation). The HTTP error doesn't mean "nothing happened". Always re-read balance after an order exception; if quantity grew, the fill is real and must be reconciled.
- **L13 — Wallet abstraction lies.** A synthesised `spot.<asset>.free = trade + main` looks right but isn't: spot orders execute against `trade` only. Aggregating multiple sub-wallets into a single number hides which wallet the order book can actually spend. Either physically consolidate the wallets BEFORE every cycle (the chosen approach), or surface the per-wallet breakdown in the diagnostic; never present a single "free balance" derived from non-spendable sources.
- **L14 — Pseudo-tokens in spot balance responses.** Binance returns `LDUSDT`, `BFRBUSDT`, etc. in `fetch_balance` — these are Earn / Lending pseudo-tokens, not tradable. The bot tried to "recover" them as phantom spot positions on every cycle. Filter by known venue prefixes before the phantom-recovery loop.
- **L15 — String "0" is truthy in Python before float-parsing.** Binance's PM balance response returns balances as strings. An OR-chain like `r.get('crossMarginFree') or r.get('umWalletBalance')` short-circuits on the first truthy string — which includes the string `"0"`. The bot saw `$0.10` instead of `$30` for weeks. Rule: parse to numbers first, then combine; never trust truthiness of strings.

### State management

- **L16 — Phantom state is the enemy.** Any state that exists on the venue but has no row in the bot's DB is invisible to every reconciler, every gate, every monitor. The moment the bot detects an unexpected balance (e.g., a partial spot fill the order error path didn't capture), it MUST persist a `naked_spot` (or equivalent) row immediately, before attempting any recovery. The portfolio view is broken otherwise.
- **L17 — Stale reconciliation is mandatory.** A persisted position can outlive its underlying balance (operator sold externally, dust got swept, Earn got auto-redeemed). The bot must check at every cycle that DB state matches venue state for every open row, and auto-close the orphans. Without this, the dashboard shows positions that don't exist anymore.
- **L18 — Cache + force-refresh on every state mutation.** Balance fetches are cached for rate-limit reasons. After EVERY transfer, swap, or order placement, the cache must be invalidated explicitly or the next read returns pre-mutation data. The bug surface is "the bot says it has $9.90 but the venue says $0.10 — and the bot is using the stale value to make sizing decisions".
- **L19 — Idle-cycle gating.** Wallet rebalance is expensive (multiple API calls, multiple venue acks). On an idle cycle (no candidates passed the scan), rebalancing produces a drain↔rebalance oscillation that burns ~21k log events / day for zero benefit. Rebalance MUST be gated on "at least one candidate passed the scan AND needs both wallets funded".
- **L20 — Identical-error dedup.** A venue can return the same error repeatedly when a condition persists (e.g. genuinely insufficient balance for a transfer). The log layer must throttle identical-message errors per pattern; otherwise a single failing condition produces tens of thousands of events that drown the genuine signal.

### Recovery

- **L21 — Hedge before flat-close.** When recovering an orphan spot leg, the right first action is to try to hedge with the matching perp short — if the forward profitability gate passes, the orphan becomes a real position. Only fall back to selling the spot when hedging isn't feasible (no perp listing, insufficient depth, gate fails). Flat-closing as the first action throws away a profitable opportunity.
- **L22 — Dust below MIN_NOTIONAL is unsellable through normal orders.** Use the venue's dust-conversion endpoint (Binance dust → BNB, KuCoin dust → KCS). Otherwise small balances accumulate forever and trip "balance insufficient" rejections every cycle.

### Configuration

- **L23 — Per-strategy configs from day one.** A single global config row worked when only one strategy existed but rotted the moment a second strategy needed different thresholds. Bake the per-strategy / global split into the schema before adding the second strategy, not after. The merge proxy pattern (per-strategy fields override global) keeps call sites simple.
- **L24 — Migration cursor + idempotent value transforms.** One-shot data migrations (e.g. "multiply per-period rate by 1095 to get APR") must be gated by a persisted version cursor, not by a sentinel value check. Sentinels can re-fire on corrupted DBs or absurd values.
- **L25 — Additive-only schema migrations.** Never drop columns. Code may stop reading them; columns stay forever. Avoids data loss when rolling back a deploy that changed the schema. Cost: a few KB of unused columns. Worth it.
- **L26 — Form back-compat windows.** When renaming a field, accept both the old and new form-input names for at least one release cycle. Otherwise a cached browser tab POSTs the old name and gets a 422.

### Monitoring & ops

- **L27 — Heartbeat, not anomaly-triggered alerts.** The monitor chain MUST post something every cycle (every 3h), not only when anomalies fire. Silence is ambiguous: "no anomalies" and "the workflow is broken" look identical from the outside. The heartbeat is the proof of life.
- **L28 — Webhook subscription is session-scoped.** Subscribing the monitor chat to the tracker issue happens once per session and ends when the session closes. The runbook needs to mention this; otherwise the operator wonders why notifications stopped.
- **L29 — The single-instance host is a single point of failure.** SQLite on local NVMe with no auto-backups means a host failure wipes the entire trade history. Either enable host-level snapshots or run an off-host backup cron. Cost: ~$1/month. Pretending this isn't a problem cost serious trade history.
- **L30 — PR-via-MCP, not direct push.** When the deployment infrastructure blocks direct `git push origin main`, every change must go through a PR. The MCP API path works where the git proxy doesn't. Don't fight it; embrace it.
- **L31 — Bind the doc to the code.** Every behavior-changing commit must update this doc in the same commit. Without that policy, the doc rots in days. The monitor chat reads the doc on every wake-up; if the doc is stale, the monitor diagnoses the wrong reality.

### Display

- **L32 — Naked positions are first-class.** Any exposure on a venue must show on the portfolio view, even if its origin is a partial-fill orphan or an external transfer. Hiding them under "open positions only with a DB row" makes the operator's mental model inconsistent with the actual account balance. Operator's quote: "Any position is the position!"
- **L33 — Show the math the gate did.** Rejection log lines must show the comparison the gate actually performed. A line saying "net 11.57% < 10%" is fine; a line saying "11.57% < 10%" without "net" looks like a bug because the math doesn't check out at first glance.
- **L34 — Per-position thresholds in per-position rows.** Once thresholds are per-strategy, the dashboard's open-positions table should show each row's strategy threshold inline. Showing a single global threshold in the summary card is correct (it's the default); showing it on a per-position row creates a wrong impression.

### Process

- **L35 — Audit-pass discipline.** Every doc rewrite goes through at least two audit passes. Round 1: full draft. Round 2: cross-check every claim against the actual code. Round 3 (optional): readability + cross-reference consistency. In this session, the round-2 audit caught a NameError regression (`rt_basis_bps`) AND three "active config fields" that were in the doc but never actually read by the bot. Doc audits find code bugs.
- **L36 — Operator vocabulary > internal naming.** Page names ("Dashboard", "Configuration", "Safety & Rules") match how the operator thinks about the bot. Field names should too. When the developer term and the operator term diverge, the operator term wins on UI surfaces; the developer term can stay in the schema for compatibility.

---

## 17. Rewrite plan (core + UI from scratch)

This section is a **plan**, not a description. It captures the order of operations for replacing the entire codebase while preserving the contracts in §7 (DB schema), §8 (diagnostics endpoint), and the learnings in §16. Update it as the rewrite progresses; collapse completed stages.

### 17.1 Goals

- **Same observable behaviour** through the four external surfaces: HTTP UI, `/api/diagnostics` JSON, SQLite DB on disk, venue API calls. A user / monitor / cron should not be able to tell the rewrite happened (until they read the changelog).
- **All §16 learnings encoded** in the new code structure or test suite. The new code should fail-fast on every regression listed; ideally with a unit test per learning.
- **Cleaner module boundaries** so future strategies (cross-venue, onchain) are additive, not invasive.
- **Lossless data migration.** Existing positions, trades, equity curve, capital flows carry over to the new code without conversion.

### 17.2 Non-goals

- Deployment infrastructure (Vultr host, Coolify pipeline, GitHub workflow) stays. The rewrite is application code only.
- The `/api/diagnostics` JSON contract stays byte-for-byte (§8.1). The monitor cron + tracker issue + dependent automation must keep working through the cutover.
- The DB schema contract stays (§7.1.1). Additive changes are fine; renames or drops are not.
- The page taxonomy and operator vocabulary in §5 stay. URL paths stay (`/dashboard`, `/config`, `/safety`, `/logs`, `/transactions`, `/monitoring`).
- The doc-update policy in §13 stays binding through the rewrite — including changes to this rewrite plan section.

### 17.3 Scope

- **Bot core**: cycle loop, scanner, gates, executor, position state machine, recovery flows, venue adapters.
- **UI surface**: HTTP routes, HTML pages, form handlers, action endpoints, diagnostics endpoint, monitoring/safety surfaces.

Out of scope for this rewrite (separate efforts):
- Cross-venue arb strategy implementation (planned, but separate PR).
- Onchain strategy implementation (planned, but separate PR).
- Schema-level changes (e.g. per-strategy split of mode_state) — additive if needed, but not a goal here.

### 17.4 Stages

The rewrite proceeds in ordered stages. Each stage has explicit **entry criteria** (what must be true before starting) and **exit criteria** (what must be true to advance). Don't skip — each gate exists to catch a class of regression.

#### Stage 0 — Freeze + snapshot

**Entry criteria:** none.

**Work:**
1. Tag the current main branch `pre-rewrite-2026-05-12` (or similar). This is the rollback anchor.
2. Snapshot the production DB (off-host backup). Tag it.
3. Confirm §7.1.1 + §8.1 contracts are accurate against the running code. Any divergence → fix the doc OR add an additive column to the contract, not the other way around.
4. Write a parity-test harness: a script that hits the current `/api/diagnostics`, dumps the response, then will hit the new endpoint and diff. The diff must be empty modulo timestamps for cutover.

**Exit criteria:**
- Tag exists in git. Snapshot exists off-host (Vultr backup OR scp to a second host — see §16 L29).
- Parity-test harness runs against the current bot and produces a baseline dump.
- §7 + §8 contracts confirmed accurate.

#### Stage 1 — New repo skeleton + DB layer

**Entry criteria:** Stage 0 done.

**Work:**
1. Greenfield directory layout. Suggested boundaries (revise as needed):
   - `core/` — domain entities (Position, Trade, Strategy config), pure-logic math (gates, basis formulas).
   - `gateways/` — one module per venue, implementing the same protocol (read funding, walk book, place order, transfer, dust-convert). Per-venue learnings (L11) encoded as comments + per-venue tests.
   - `loop/` — cycle orchestrator: Phase A safety → Phase B exits → Phase C entries → post-cycle. Idle-cycle gating (L19) built in.
   - `state/` — DB models + migrations. Schema matches §7.1.1.
   - `web/` — FastAPI app, routes, templates. Imports core + state.
   - `diagnostics/` — `/api/diagnostics` endpoint + anomaly rules. Imports state.
2. Reimplement the DB layer matching §7.1.1 byte-for-byte. Include the additive migration pattern from §7.3.
3. Verify against the production snapshot from Stage 0: load it into a fresh deployment of the new code, assert that the bot can read every existing row without coercion.

**Exit criteria:**
- New repo passes lint + typecheck.
- New DB layer round-trips the Stage-0 snapshot losslessly.
- Bot can boot to "idle, waiting for cycle" with the snapshot loaded — no exceptions.

#### Stage 2 — Gateways (one venue at a time)

**Entry criteria:** Stage 1 done.

**Work, per venue:**
1. Implement the venue gateway protocol: read funding rates, simulate fill, place limit-IOC (spot + perp), transfer (spot↔futures), consolidate wallets, dust-convert, account-mode probe.
2. Bake in the per-venue quirks documented in §6 + §16 L11:
   - **Binance**: PM-only routing, per-currency futures balance, dust → BNB endpoint, `crossMarginFree + umWalletBalance + cmWalletBalance` aggregation, NEVER use OR-chain on string fields (L15).
   - **KuCoin**: Classic wallet model (main/trade/margin/isolated/contract/pool), futures-side transfer for OUT direction (L11), two-hop drain (§6.2), per-currency futures balance fetch, book-depth limit must be 20 or 100, dust → KCS endpoint.
3. Unit tests covering the failure modes in §10 + §16 L11–L15 + L18.

**Exit criteria, per venue:**
- Read-only paper-mode cycle runs cleanly against the venue (no orders placed).
- All §16 venue-specific learnings have a corresponding test or assertion.
- Diff against the Stage-0 parity dump: `/api/diagnostics` `wallets` section matches the old bot's output for the same account state.

Do Binance first (smaller wallet surface), KuCoin second (Classic wallet quirks).

#### Stage 3 — Cycle orchestrator (paper mode)

**Entry criteria:** Stage 2 done for both venues.

**Work:**
1. Implement the cycle in §3.1 SOP, phase by phase. Each phase is a function the orchestrator calls in order.
2. Implement the math in §3.1 — gates, signed basis, worst-case adverse swing, reservation clamp. Unit tests for every formula in §3.1 math (use the worked examples in the doc as test vectors).
3. Tier separation (L10): Tier-1 in the scanner, Tier-2 in the book walk, Tier-3 in the gate.
4. Idle-cycle gating (L19): wallet rebalance only runs when at least one candidate passed Tier-1.
5. Position state machine: open ↔ naked_spot ↔ closed, transitions per §7.2.
6. Naked-spot recovery (L16, L21): hedge-before-sell + stale reconciliation + dust sweep.
7. Outer exception handler that logs `Loop iteration error` and continues — BUT also a NameError-prevention measure: every cycle entry-point function has at least one import-time symbol resolution check (L02).

**Exit criteria:**
- Paper mode runs cleanly for 24h on the parity test harness.
- All §3.1 math tests pass.
- All §10 failure modes have a corresponding handler + test.
- Diff against Stage-0 paper-mode snapshot: every per-cycle event in `bot_events` is reproducible (same gate decisions, same rejection categories with same reasons).

#### Stage 4 — UI

**Entry criteria:** Stage 3 done.

**Work:**
1. FastAPI routes matching §5 paths (`/health`, `/dashboard`, `/transactions`, `/logs`, `/monitoring`, `/config`, `/safety`, `/api/diagnostics`).
2. HTTP Basic auth on everything except `/health` and `/api/diagnostics`. Auth dependency-injected, never inlined.
3. Templates matching §5.2 purposes. Naked-spot rendering rule (L03, L32). Per-position thresholds shown inline (L34). Gate-math displayed in rejection messages (L33).
4. Form-handling conventions (§5.4): `303 See Other` redirects, percent fields, back-compat alias accepts.
5. Action endpoints (§5.5).
6. `/api/diagnostics` endpoint matching §8.1 byte-for-byte.

**Exit criteria:**
- Every route in §5.2 returns `200` smoke-tested.
- Parity test harness: `/api/diagnostics` response from new code equals old code's response on the same DB snapshot (modulo timestamps).
- HTML pages render correctly with naked-spot, multi-strategy, and various edge-case states present.

#### Stage 5 — Live mode validation (paper-only first, then dry-run)

**Entry criteria:** Stage 4 done.

**Work:**
1. Deploy new code in PAPER mode only. Run alongside the production bot (read-only on live wallets — paper sends no real orders).
2. Compare paper-mode decisions across the two bots for 48 hours. Differences must be intentional (e.g. fixed bugs in the new code) or zero.
3. After 48h clean: enable live mode on a single venue, single strategy. Monitor closely.
4. After 48h clean live: enable the second venue.

**Exit criteria:**
- 48h paper-mode parity (or all deltas intentional + documented).
- 48h live single-venue clean: no naked positions accumulated, no error bursts, all trades reconcile.
- 48h live both-venue clean.

#### Stage 6 — Cutover + decommission

**Entry criteria:** Stage 5 done.

**Work:**
1. Switch the public URL / Coolify deployment to the new code as the primary.
2. Keep the old code running in a second Coolify service for 7 days, read-only on the same DB, as a fallback.
3. After 7 days clean: archive the old service.
4. Delete `pre-rewrite-2026-05-12` branch from local; keep the tag.
5. Update SYSTEM.md §15 changelog with the cutover entry. Collapse the rewrite plan in §17 to a one-paragraph "completed YYYY-MM-DD; see git log".

**Exit criteria:**
- Old service decommissioned.
- Doc updated.

### 17.5 Risk management

- **Data loss**: addressed by Stage 0 snapshot + parallel-run in Stage 5.
- **Mid-rewrite behavior drift**: the production code keeps running on `main`; the rewrite lives on a long branch (`claude/rewrite-2026-05-12` or similar). Only Stage 6 cuts traffic over. Rolling back = redeploying the pre-rewrite tag.
- **Monitor chat blind spot**: keep the diagnostics cron + tracker issue pointed at the production bot through Stage 5. The new bot's `/api/diagnostics` is parity-tested but not the primary alert source until Stage 6.
- **Capital loss from a regression**: Stage 5 enforces 48h paper-mode parity before any live orders. Single-venue, single-strategy first.
- **Doc-rot during the rewrite**: every stage exit criterion includes "SYSTEM.md updated to reflect what's now live". §13 applies through the rewrite.

### 17.6 Estimated effort (rough)

| Stage | Effort estimate |
|---|---|
| 0 — Freeze + snapshot + parity harness | 0.5 day |
| 1 — Repo skeleton + DB layer | 1 day |
| 2 — Gateways (Binance + KuCoin) | 2 days |
| 3 — Cycle orchestrator paper-mode | 2 days |
| 4 — UI | 1.5 days |
| 5 — Live validation (mostly wait time) | 3 days clock (operator-attended) |
| 6 — Cutover + decommission | 0.5 day |
| **Total** | **~10 working days** + 7 days fallback retention |

Estimates assume the rewrite is the only ongoing work. Concurrent feature development extends accordingly.

### 17.7 Status

Pre-Stage-0. Awaiting operator go-ahead.

---

> **For the monitor chat:** Always read this doc from the latest `main` before judging anomalies. The definitions (§0), strategy SOP + math (§3), rejection categories (§9), failure modes (§10), response policy (§11), and learnings (§16) are your operating manual. When a new pattern emerges in production, add a learning to §16 in the same PR that ships the fix. If a rewrite is in progress (§17), be aware that the new code may behave differently from the old in documented ways — check the changelog before flagging differences as anomalies.
