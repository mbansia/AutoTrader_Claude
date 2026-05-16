# AutoTrader_Codex — System SSOT

The single living source of truth for what this bot does and how it works. Every behavior-changing PR updates the relevant section in the same commit. The diagnostics-monitor chat re-reads this on every wake-up before judging anomalies; any new dev reads it once to onboard.

> Status: **v1.5** — 2026-05-14 Hyperliquid added as a third venue (v1.4 → v1.5): DEX wallet model, EVM auth, **hourly funding**, single unified USDC pool. See changelog §15.

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
- [18. Open implementation gaps](#18-open-implementation-gaps-for-the-next-audit-pass)

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
| **Market order** | "Buy/sell the full quantity now at whatever price the book offers." Fills immediately at the volume-weighted average of the book up to the requested size. **The bot's primary order type** (v1.4): combined with pre-trade closed-form depth analysis (§3.1 math) and **FOK** semantics, the market order's slippage is bounded by the same depth the sizing math already saw, and the order fires with **zero latency window** for other actors to react. The historical concern about market orders ("too much slippage on thin books") is resolved by sizing to the available depth first. |
| **Limit order** | "Buy/sell at price X or better." Fills against existing orders that cross your limit. Can rest on the book waiting if nothing crosses. |
| **IOC** (Immediate-Or-Cancel) | A time-in-force flag. Order fills whatever depth it can against existing book at the moment of arrival, then immediately cancels any unfilled remainder. **Never** leaves a resting order. |
| **FOK** (Fill-Or-Kill) | A time-in-force flag. Order fills the **entire** requested quantity at once, or cancels with zero fill. No partial fills, ever. **The bot's preferred TIF on every leg** (v1.4): a partial fill creates a naked-leg recovery problem; FOK eliminates the partial-fill class by construction. Trade-off: more clean rejects on thin books, but every accepted order is guaranteed hedged. **Venue support is uneven**: Binance USDM-futures + KuCoin futures accept `type=market, timeInForce=FOK` directly. **Binance spot does not** accept `timeInForce` on `type=market`; the standard arb-trader fallback is "**marketable-limit + FOK**" — submit a limit order with a price set far beyond top-of-book (e.g. 1% above ask for a buy) and `timeInForce=FOK`. Effectively a market order with FOK semantics. The gateway abstraction must transparently choose `market+FOK` or `marketable-limit+FOK` per venue+symbol. |
| **OCO / atomic bracket** | A multi-leg order primitive where two or more legs are accepted as a single ticket and settle (or reject) together. **Preferred wherever the venue supports it** for spot-leg + perp-leg pairing, because it eliminates the inter-leg gap entirely. Today's reality: most CEX APIs expose OCO only within a single book (e.g. perp with stop + target), not across spot ↔ perp on the same account. Use it when the venue offers it; fall back to **parallel market+FOK** legs otherwise. |
| **Limit-IOC** | **DEPRECATED (v1.4).** Was the bot's only order type pre-v1.4. Replaced by market+FOK because: (a) limit-IOC's price guarantee requires a tick buffer that on thin books exceeds typical market slippage; (b) limit-IOC produces partial fills under thin-book or mid-fill-error conditions, which create naked legs (§10); (c) limit-IOC orders are still always takers (no fee benefit) and the round-trip latency from book-walk → limit-construction → submission gives front-running actors a window market+FOK closes. See §16 L39. |
| **Taker fee** | The fee charged when an order crosses the existing book (i.e. removes liquidity). All bot orders (market or any IOC variant) are takers. Typically 0.06–0.10% per fill on these venues. |
| **Maker fee** | The fee (sometimes a rebate) for an order that rests on the book waiting to be crossed. The bot does NOT use maker orders — incompatible with the same-cycle entry/exit discipline. |
| **Reservation** | When a venue accepts an order, it immediately reserves the cash the order could spend. For market-buys with FOK, reservation = `qty × top_of_book_ask` (some venues add a small buffer). The reservation is released atomically when the order completes or cancels. Sub-target sizing (§3.1 math) ensures reservation never overflows. |
| **Tick size** | The minimum price increment a venue accepts for orders on a given symbol. E.g. 0.0001 USDT. Limit prices off-tick are rejected; market orders are immune. |
| **Lot step / step size** | The minimum quantity increment for a symbol. Quantities off-step are rejected. The closed-form sizing math (§3.1) floors target_qty to this. |
| **MIN_NOTIONAL** | The venue's per-symbol minimum order value in quote currency. Below this, the venue rejects the order. Roughly $5 on Binance, $1 on KuCoin. Dust below this can't be sold through normal orders — must use the venue's dedicated dust-conversion endpoint. |
| **Sub-target sizing factor** | A multiplier `< 1.0` applied to the closed-form max fill size before order submission (default 0.75, v1.4). Leaves headroom for: (a) the matching engine reserving slightly more than our model expects, (b) book moves between our snapshot and the order, (c) other actors' orders landing in our reservation gap. Trade-off: ~25% smaller positions for near-zero reject rate. |

### 0.3 The bot's strategy (long-spot / short-perp funding arbitrage)

| Term | Definition |
|---|---|
| **Long spot + short perp** | The bot's only active structure. Buy the base asset on spot; simultaneously short the same quantity of that asset's perp. Net price exposure = 0. The position earns funding payments every funding window while the perp's funding rate is positive. |
| **Entry basis** | The basis at the moment we open: `(perp_sell_fill_price − spot_buy_fill_price) / spot_buy_fill_price × 10000`, in bps. Positive entry basis = we sold the perp leg at a premium relative to where we bought the spot leg = we pocketed that gap as entry profit. |
| **Worst-case adverse exit basis** | A conservative assumption: "by the time we close, the basis will have moved against us by `m × |entry_basis|` bps", where m is a multiplier (default 3.0). For long-spot / short-perp, "adverse" means basis moves further positive — we sell our spot cheap relative to where we have to buy back the perp. |
| **Position leg** | Either the spot side or the perp side of a single delta-neutral position. Each position has two legs that should be equal in absolute quantity at all times. |
| **Naked leg** | A leg that lost its counterpart. Naked legs are unhedged and exposed to price moves; the bot tries to recover them every cycle. Has two symmetric directions — naked spot and naked perp (below). The recovery logic must treat both as first-class; assuming only one direction is the brittleness §16 L21 calls out. |
| **Naked spot** | A spot holding the bot owns but with no matching perp short. Net exposure: **long base asset, no hedge** (price down → loss). Persisted as a position with status `naked_spot`. Origin: an entry path filled the spot leg, then the perp short failed (error, zero-fill, partial). See §10. |
| **Naked perp** (also "naked futures") | An open perp short with no matching spot holding. Net exposure: **short base asset, no hedge** (price up → loss). Persisted as a position with status `naked_perp`. Origin: an exit path closed the spot leg (sold), then the perp buy-back failed; or an entry/exit-phase glitch left a perp short on the venue with no spot backing. Symmetric to naked spot — same recovery model in the opposite direction. See §10. |

### 0.4 Position lifecycle

| Term | Definition |
|---|---|
| **Position** | A row in the bot's database recording one delta-neutral pair (one spot leg + one perp leg) on one venue under one strategy. Has a status: `open`, `naked_spot`, `naked_perp`, or `closed`. |
| **Open position** | Both legs are live on the venue and the position is earning funding. The dashboard's "Open positions" table shows these. |
| **Naked-spot position** | Spot leg exists, perp leg does not. Sub-state of "open" for accounting (counts toward exposure) but flagged for recovery on the next cycle. Once hedged or sold back, the row transitions out of `naked_spot`. |
| **Naked-perp position** | Perp short exists, spot leg does not. Symmetric to naked-spot. Sub-state of "open" for accounting; flagged for recovery — try to hedge by buying matching spot (if forward profitability passes), else buy back the perp short to flatten. |
| **Closed position** | The position has been fully closed (both legs flat) and the realized P&L is locked in. Visible in the dashboard's "Closed positions" history. |
| **Currently exposed** | The combined set `{open, naked_spot, naked_perp}` — every "currently exposed" query in the bot uses this set. Either naked direction counts toward portfolio exposure exactly like an open position because the unhedged leg has real price risk. |

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
| **DEX wallet model** (Hyperliquid) | The "account" is an Ethereum-style address. There are no sub-accounts and no wallet types — a single USDC collateral pool funds both spot and perp positions. Auth is by ECDSA signature (operator's private key signs every order client-side); there is no API key / secret. Deposits + withdrawals are L1 transactions on Arbitrum, not venue-side journal entries. |
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
| **Wallet cap** | The hard ceiling on a trade's size imposed by available cash. Post-v1.4 the cap is computed per-side in the closed-form sizing math (§3.1 math) — for buy-spot the cap is price-dependent (per-level); for sell-spot and perp legs it's a flat cap derived from base balance or mark-price margin. The `sub_target_sizing_factor` then reduces the result by ~25% to absorb book moves and reservation buffers. |
| **Target quantity** (target_qty in formulas) | The base-asset quantity the bot will submit on the next order. Computed in a single closed-form pass per book (§3.1 math) — no iteration. |
| **Reservation** | The cash a venue holds against an in-flight order. For market+FOK (post-v1.4) this is approximately `qty × top_of_book_price + small_venue_buffer`; held for the few hundred ms the FOK takes to resolve, then released. Sub-target sizing keeps the reservation comfortably under wallet_free. See §16 L07 (legacy context). |
| **Safety factor** | A multiplier (~0.99) applied to free-balance ceilings to absorb fee accrual, rounding, and last-millisecond balance shifts. Trades a tiny bit of headroom for reservation-rejection insurance. |
| **Mandatory vs voluntary exit** | **Voluntary** exits (forward profitability dropped below exit threshold) are deferred for one cycle if closing right now would print an extra cost (unfavourable live basis). **Mandatory** exits (stop-loss, hedge integrity, market unhealthy, basis dislocation) close immediately regardless. |

### 0.9 Operational concepts

| Term | Definition |
|---|---|
| **Diagnostics endpoint** | An auth-gated JSON endpoint (§8) that returns a structured snapshot of cycle health, positions, wallets, recent events, recent trades, and rule-based anomalies. Polled by an external cron every 3 hours and by humans / monitor agents on demand. |
| **Anomaly** | A rule-based flag produced by the diagnostics endpoint when the bot's state diverges from healthy steady-state — e.g. no events in the last hour, naked leg (spot or perp) older than 1 hour, error burst, etc. |
| **Heartbeat tracker** | A persistent GitHub issue that the diagnostics cron updates every 3 hours regardless of anomaly state. Posting a comment per run guarantees a webhook fires every cycle so the monitor chat can confirm "the bot's still alive". |
| **Monitor chat** | A dedicated Claude session subscribed to the heartbeat tracker. Reads this doc on every wake-up before judging anomalies. Responds inline on the tracker with one-line acknowledgements on clean runs, full diagnosis on anomaly runs, or PRs when it detects a code regression. |

### 0.10 Implementation primitives (the stack this spec assumes)

The spec is intentionally code-agnostic in §3 onward, but a from-scratch implementer needs to know which stack the operational details were written against. Picking a different stack is allowed, but the venue quirks in §6 and §16 L11 are described in ccxt's vocabulary and will need re-derivation.

| Layer | Choice | Why it matters |
|---|---|---|
| **Language** | Python 3.11+ | Every formula, error class, datetime, and concurrency primitive in this doc assumes Python semantics. |
| **Venue SDK** | **ccxt** (unified spot + futures wrappers) | The "venue quirks" catalog (KuCoin `limit=20|100`, Binance PM `/papi/v1`, two-hop transfer, `LDUSDT` filter) are documented as ccxt deviations. A non-ccxt rewrite must re-discover these from venue docs. |
| **Web framework** | FastAPI + Jinja2 templates, no SPA | §5 page taxonomy + form-handling conventions assume server-rendered HTML with `Depends(auth)` dependency injection. |
| **DB / ORM** | SQLite + SQLAlchemy (declarative `Base`), additive migrations applied at startup | §7 schema is written as SQLAlchemy column types; `DATABASE_URL` env var swaps the backend. |
| **Concurrency** | One in-process loop thread per (mode, gateway). No multi-process, no async venues. | The bot relies on serialised access to ccxt clients and the SQLite write path. |
| **Datetime discipline** | All persisted timestamps are **UTC-aware** (`datetime.now(timezone.utc)`); all `/api/diagnostics` outputs use ISO-8601 with `Z` suffix. Never use naive `datetime.now()` — silently corrupts time-based queries on hosts whose locale isn't UTC. | |
| **No JS build step** | Vanilla JS in `app/static/`; Nixpacks runs a Node phase that does nothing | Keeps the deploy story trivial; a from-scratch UI should preserve this. |

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
| `HYPERLIQUID_WALLET_ADDRESS` / `HYPERLIQUID_PRIVATE_KEY` | Hyperliquid EVM auth. Operator's wallet — same sensitivity as an API secret. |
| `BINANCE_EXPECTED_ACCOUNT_ID` / `KUCOIN_EXPECTED_ACCOUNT_ID` / `HYPERLIQUID_EXPECTED_ACCOUNT_ID` | Boot-time account-id assertion (§3.1 mitigation policy). Refuse to start on mismatch. |
| `BOT_WORKER_ENABLED` | `"1"` (default) starts the background cycle threads on app startup; `"0"` for API-only replicas (HTTP UI without trading). v1.5. |
| `ACTIVE_EXCHANGES` | Comma-separated whitelist of venues to spawn workers for, e.g. `binance,kucoin`. Default: every venue whose creds are present, **except** Hyperliquid (must be opted in explicitly because of its hourly-funding + EVM-key-compromise-is-catastrophic profile — §6.4 + L11). v1.5. |
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

This bot's only active trade: pick a perp paying high positive funding, **buy the base asset on spot** and simultaneously **short the same base on the perp** in equal quantity. Net price exposure is zero (whatever spot does, the short perp does the opposite), so the position doesn't care if the underlying goes up or down. Each funding window, the short perp **receives** the funding payment from the long perp side. The position holds until either the **forward profitability** falls below the exit threshold (voluntary exit) or the **spot↔perp price differential dislocates** beyond the configured ceiling (mandatory exit). There is no time-based exit.

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

Costs and income per 4h window:

| Item | Value |
|---|---|
| Funding income | +0.025% = +2.5 bps |
| Worst-case adverse basis (m = 3 × \|entry\| = 90 bps), charged as a per-window cost (see "why per-window" below) | −90 bps |
| Round-trip taker fees (2 × spot + 2 × perp, ~6 bps each), also charged per-window | −24 bps |
| **Net per 4h window** | **−111.5 bps** |

Annualised: `(1 + net_per_window)^(periods_per_year) − 1` where `net_per_window = −0.01115` and `periods_per_year = 24×365/4 = 2190`. Result is deeply negative. The gate rejects.

Now a higher-funding candidate at 0.5% / 4h with the same +30 bps entry basis:

| Item | Value |
|---|---|
| Funding income | +50 bps |
| Worst-case adverse basis | −90 bps |
| Round-trip fees | −24 bps |
| **Net per 4h window** | **−64 bps** |

Still rejected at per-window accounting. The signal: under a *strictly* per-window cost charge, only very-high-funding candidates clear the gate — exactly the safety property the operator wants.

**Why "per-window" cost, not "amortised over expected hold"?** Amortising 114 bps over (say) 18 expected windows held = 6.3 bps/window would *seem* more accurate (50 − 6.3 = 43.7 bps net → ~16,000% APY, cleared). But the bot has no commitment to hold for 18 windows: a stop-loss or hedge-integrity exit can fire after one window. Per-window accounting is the conservative choice that survives early exits. The gate is intentionally stricter than the expected case so a candidate that clears it is robust to bad scenarios, not just the average.

#### SOP per loop iteration

Loop period: globally configured (default 30s). Runs separately for paper and live mode, on each active venue.

```
Phase A — Safety (live only — but paper mode still runs the READ-ONLY
                 portions: venue probes, balance fetches, state inspection;
                 only ORDER PLACEMENT is paper-side synthetic)
   For each open position:
      market-health check     → if the venue reports the perp's market as
                                delisted, halted, or missing from the live
                                markets list, force-close both legs via the
                                normal close path (market+FOK, parallel —
                                same path as a voluntary exit)
      hedge-integrity check   → detection rule:
                                  spot leg present iff
                                    spot_wallet[base].free + .used ≥ qty − ε
                                  perp leg present iff
                                    a non-zero open-perp position exists on
                                    the venue for perp_symbol with the same
                                    sign and qty within ε
                                If exactly ONE leg is present → close the
                                surviving leg (same market+FOK close path);
                                mark the position closed with last_close_error
                                describing which leg vanished.
                                If BOTH legs missing → mark closed (orphan
                                cleanup; the position rowed-out via two
                                external interventions).
   Maintenance-mode handling (if mode_state.maintenance_mode is true):
      For every currently-exposed position (status in OPEN_STATUSES):
        run the normal close path (market+FOK, parallel, profitability
        deferral DISABLED because maintenance is a mandatory exit). Skip
        the profitability gate and the deferral logic; the operator wants
        out.
        Note (v1.4): the pre-v1.4 spec banned market orders here. With
        closed-form sizing + sub-target-sizing-factor (§3.1 math), the
        slippage that earlier banned market orders is bounded by the same
        depth the sizing math already saw, so market+FOK is now safe and
        materially faster than limit-IOC. See §16 L38–L40.
        Entries are blocked for the duration (no Phase C).
   Phantom-leg recovery (sweep BOTH wallets, look for orphaned legs in either
   direction — naked spot AND naked perp are symmetric failure modes):
      ① Stale reconciliation   any naked-spot or naked-perp position whose
                                underlying leg has disappeared from the venue
                                (spot balance gone / perp short closed
                                externally) → mark closed
      ② Naked-spot branch — for each non-stable spot asset present in the
         wallet with notional ≥ tracking floor (≈ $0.10):
           - if notional < venue MIN_NOTIONAL → persist as naked-spot dust;
             skip recovery this cycle, defer to dust sweep
           - else: persist as naked-spot, then either
               Phase 1: try to hedge by SHORTING the matching perp (if
                        forward profitability gate passes at current funding); or
               Phase 2: SELL the spot back to the quote stablecoin via
                        market+FOK
      ③ Naked-perp branch — for each open perp short on the venue without a
         matching spot holding (or matching DB row):
           - persist as naked-perp, then either
               Phase 1: try to hedge by BUYING matching spot (if forward
                        profitability gate passes at current funding); or
               Phase 2: BUY BACK the perp short via market+FOK to flatten
         Note: today's entry flow places spot first, perp second — so
         naked-perp is rare from entries (it would require an exit-time perp
         buy-back failure, or an externally-opened short). Recovery logic
         must still handle it; assuming only one direction is L21 brittleness.
      ④ Dust sweep: convert all naked-spot-dust positions to the venue's
         native fee token (BNB / KCS) via the venue's dust-conversion
         endpoint; mark each converted position closed. (Perp shorts have no
         dust equivalent — sub-min perp shorts are simply bought back at the
         next opportunity.)

Phase B — Exits (when exit is enabled for this mode/strategy)
   Fetch fresh predicted funding rates per venue.
   For each open position:
      update last-known funding rate + funding interval
      compute forward-looking net APY at LIVE funding + live basis (math below)
      exit triggers (whichever fires first):
         forward_profit_below_threshold   (forward net APY < exit threshold)
                                          → VOLUNTARY (deferrable on unfavourable basis)
         basis_dislocation                (|live_basis − entry_basis|
                                          > basis_dislocation_exit_bps)
                                          → MANDATORY: the cost model that
                                          approved entry no longer holds; exit
                                          before the assumption breaks worse.
         stop_loss                        (mark-to-market PnL / entry notional
                                          ≤ stop-loss-pct)
                                          → MANDATORY
      Voluntary exits DEFER for one cycle if the live basis is currently
      unfavourable for closing (would print extra cost beyond max_exit_basis_bps).
      Mandatory exits (stop-loss, basis-dislocation, hedge integrity, market
      unhealthy, maintenance-mode) close immediately regardless of live basis.
      There is NO time-based / max-hold exit — the strategy holds as long as
      forward economics + basis sanity both pass.

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
      Closed-form sizing (single pass, no iteration — see §3.1 math):
         Snapshot both books (spot + perp) ATOMICALLY (websocket frame or
            back-to-back REST in <50ms — see §16 L42).
         Compute binding-level qty per book by walking each book one pass:
            For each level i:
               cum_qty_i      = sum of depths from level 1..i
               max_fund_qty_i = (wallet_free × safety) / (price_i)
               feasible_i     = min(cum_qty_i, max_fund_qty_i)
            Binding level = the i that maximises feasible_i.
            book_binding_qty = feasible at binding level.
         target_qty = min(spot_book_binding, perp_book_binding × leverage,
                          sized_notional / spot_mid) × sub_target_sizing_factor
         Floor target_qty to lot step.
      Tier-3 profitability gate (full math below).
         Forecast avg fill = vwap of book up to target_qty.
         Use forecast in the profitability formula.
         REJECT insufficient_annualized_profit if net APY < entry threshold.
      ORDER PLACEMENT — venue-tier preference, in order:
        (A) Atomic spot+perp bracket (where the venue supports it
            cross-book; rare today): submit as one ticket. Either both fill
            or both reject. Done.
        (B) Parallel market+FOK on each leg (default for current venues):
            Fire BOTH orders concurrently (asyncio.gather / thread pool).
            Each order: type=market, time_in_force=FOK, qty=target_qty.
            Wait for both to settle (typically <500ms).
            Outcome cases:
               BOTH FILL    → persist position status=open; record both
                              trades; success.
               BOTH REJECT  → reject candidate; reason carries both errors;
                              retry next cycle. (Common: book moved through
                              FOK depth simultaneously on both sides — a
                              fair signal that conditions changed.)
               ONE FILLS    → orphan handling:
                  - leg that filled rolls back via a same-cycle market+FOK
                    in the reverse direction at the smallest needed size.
                  - if rollback succeeds → reject with single_leg_orphan,
                    no naked row persisted.
                  - if rollback fails → persist as naked_spot or naked_perp
                    per §0.4; phantom-leg recovery (Phase A next cycle)
                    handles it (L21 / §3.1 Phase A).
      Each order is submitted with a venue-side client-order-id derived
         deterministically from (cycle_id, leg, candidate_symbol) so a
         network-retry of the same call is idempotent (§16 L43).
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

Two additional **mandatory** exit triggers, evaluated alongside:

- **Basis dislocation**: `(b_l − b_e) > basis_dislocation_exit_bps` → MANDATORY exit. Rationale: the entry gate's worst-case basis cost is `m × |b_e|`. When the live basis has actually moved that far adverse (or close to it), the cost model that approved entry no longer describes the trade. Exit while the dislocation is bounded rather than wait for it to widen further. Default `basis_dislocation_exit_bps = 50.0` bps; calibrated to fire at roughly the gate's `m × |b_e|` worst-case for a typical 15-bp entry basis. Operators can tune per strategy.
- **Stop-loss**: `unrealized_PnL / spot_entry_notional ≤ stop-loss-pct` → MANDATORY exit (not deferrable on adverse basis).

**There is no time-based / max-hold exit.** A position holds for exactly as long as forward economics and basis sanity both pass. A position with persistent high funding and stable basis can run for weeks; a position whose funding decays in hours exits in hours. Time is not a meaningful axis here — money is.

Voluntary exits (forward-profit) **defer** for one cycle when the live basis would print a closing cost above the configured exit-basis ceiling (`max_exit_basis_bps`). The deferred exit retries every cycle until basis becomes favourable. Mandatory exits (basis-dislocation, stop-loss, hedge-integrity, market-unhealthy, maintenance-mode) are NOT deferrable.

#### Closed-form sizing (v1.4 — replaces the walk loop)

**The iterative walk was a solver, not a strategy.** A book is a sorted list of `(price, depth)` levels; the constraint set (wallet reservation × leg + cumulative depth) is monotone in the level index; the optimum is a single binding level per side. This is solvable in one pass per book, no fixed-point iteration. The closed-form derivation:

```
Inputs per side ("side" ∈ {spot, perp}):
   book[]            sorted by price aggressiveness
                       buy  → ascending  prices p_1 < p_2 < …
                       sell → descending prices p_1 > p_2 > …
   wallet_free       free balance in the leg's quote (or base for sell)
   safety            safety factor (default 0.99)
   leverage          1 for spot; configurable for perp (default 1)
   sub_target        sub-target sizing factor (default 0.75)

Step 1 — walk book ONCE, level by level, computing per-level feasibility:
   cum_qty_0 = 0
   for each level i with (price p_i, depth d_i):
       cum_qty_i      = cum_qty_{i-1} + d_i
       max_fund_qty_i = (wallet_free × safety × leverage) / p_i
       feasible_i     = min(cum_qty_i, max_fund_qty_i)

Step 2 — pick the binding level: i* = argmax_i feasible_i
   book_binding_qty[side]   = feasible_{i*}
   forecast_avg_price[side] = vwap(book[1..i*], up to feasible_{i*})

Step 3 — cross-leg combine:
   target_qty_raw = min( book_binding_qty[spot],
                         book_binding_qty[perp],          # perp's leverage
                                                          # already baked in
                         sized_notional / spot_mid )      # operator cap
   target_qty     = floor_to_lot_step(target_qty_raw × sub_target, lot_step)
```

**Why "argmax over feasible_i"?** As you walk deeper into the book, `cum_qty_i` grows monotonically (good — more depth). What `max_fund_qty_i` does depends on the side:

| Side | `max_fund_qty_i` shape | Binding-level rule |
|---|---|---|
| **Buy spot** (entry, hedging recovery) | Strictly decreasing in `i` (worse price → less wallet can fund) | Per-level argmax of `min(cum_qty, max_fund)`. The crossing point is the optimum. |
| **Sell spot** (exit, naked-spot recovery) | Constant: `base_balance × safety` (you already own the base; reservation is qty-only, price-independent) | `target_qty = min(cum_qty_until_filled, base_balance × safety)` — flat cap, no per-level argmax needed. |
| **Sell perp** (entry short) | Constant in `i` (initial margin is computed off mark-price, fixed at submission): `(wallet_quote × safety × leverage) / mark_price` | Same flat-cap rule. |
| **Buy perp** (exit, naked-perp recovery / close) | Constant in `i` (margin off mark-price): `(wallet_quote × safety × leverage) / mark_price` | Same flat-cap rule. |

The walk-and-argmax form applies to the **buy-spot** case (which is the entry path's binding leg on price-dependent reservation). The other three sides are flat-cap and shortcut to `min(cum, cap)`. A single helper function takes the side as input and dispatches.

**No tick-buffer, no limit-price construction.** The order type is **market** with **FOK** time-in-force (§0.2). The forecast `vwap` is what we feed into the profitability gate; the venue's actual fill comes in within tick-level slippage of that forecast on FOK semantics. `sub_target_sizing_factor` absorbs everything the safety factor used to absorb plus the small reserve-vs-mid gap the venue's reservation policy adds.

**Why `sub_target = 0.75`?** Empirically chosen to absorb:
- ~5% from venue reservation buffers above top-of-book (`reservation ≈ qty × top_ask + venue_buffer`)
- ~10% from book moves between snapshot and order arrival
- ~5% from other-actor orders racing into the same depth
- ~5% from cross-leg sizing mismatches between spot lot-step and perp lot-step

Operators can tune per strategy; lower factor = lower reject rate at smaller position size.

**Snapshot atomicity.** Both books are snapshotted within a 50ms window (websocket frame where available, parallel REST otherwise). The closed-form result is only valid if the snapshots are co-temporal; stale snapshots → forecast diverges from fill. See §16 L42.

**No iteration, no convergence concerns.** Single pass per book. The walk-loop's "4 passes" was patching the absence of closed-form derivation; with the closed-form the answer is exact in one pass and the order can be submitted immediately.

#### Per-strategy config that applies

See [§4 Configuration](#4-configuration) for the layered split. Per-strategy fields used here: entry/exit net-APY thresholds, exit-basis buffer multiplier, exit-basis ceiling, basis-dislocation exit threshold, stop-loss percent, min/max position percent of equity, sub-target sizing factor, perp leverage, auto-transfer flag, auto-quote-swap flag, futures-buffer percent. Global fields used: max open positions, max trades per day, loop seconds, paper-mode synthetic costs, hedge integrity check, delisting check. The `entry_tick_buffer_bps` / `exit_tick_buffer_bps` fields are deprecated (v1.4) — market+FOK orders have no limit price to buffer.

#### Execution-risk mitigation policy (v1.4)

The following are now policy, not options. Each was identified as a no-new-infra, no-new-API-cost mitigation for the execution-risk catalog in §10 + §16 L11–L15 + L18 + L42–L43. Implementations must enforce these on every cycle.

**Order layer:**
- All orders are **market** with **FOK** time-in-force (§0.2). Limit-IOC is deprecated. Where a venue exposes atomic spot+perp brackets, prefer them over parallel market+FOK.
- Both legs fire **in parallel** (concurrent async / threadpool), not sequentially. Eliminates the inter-leg gap that front-running actors exploit.
- Each order carries a **deterministic client-order-id** = `hash(cycle_id, candidate_symbol, leg)`. Network retries of the same call are idempotent — venue rejects duplicates.

**Sizing layer:**
- Sizing is **closed-form** (single pass per book, §3.1 math), not iterative. No 4-pass walk.
- Final qty is multiplied by **`sub_target_sizing_factor`** (default 0.75) before submission. Absorbs reservation buffers, book moves, racing actors.
- Book snapshots for spot + perp are taken within a **<50ms window** (websocket frame where the venue exposes it; back-to-back REST otherwise).

**Pre-submission freshness:**
- Predicted funding rate is re-queried at gate time (T-0 check, max age 5s). The scanner's rate is a candidate filter; the gate's rate is the binding economic input.
- ccxt market metadata (tick, lot, MIN_NOTIONAL) has a **per-cycle TTL**; on any venue reject mentioning tick/lot/min-notional, force-refresh the symbol's metadata and retry once next cycle.
- Pin the ccxt version in the lockfile. Upgrade is a deliberate event with a smoke test, never silent.

**Wallet layer:**
- Cache invalidation on every mutating call (`place_order`, `transfer`, `swap`): the cached balance is discarded before the next read returns (§16 L18).
- Wallet-consolidation is best-effort per source bucket: a per-bucket failure logs WARN and continues with the remaining buckets (partial sweep is still useful).
- Eager-seed per-strategy config rows at process startup, not on first read. Eliminates the lazy-seed race (§18 closed item).

**Account-level assertions:**
- **Account-id assertion at boot**: read each venue's account id (or sub-account uid), compare to a new env var per venue (`BINANCE_EXPECTED_ACCOUNT_ID`, `KUCOIN_EXPECTED_ACCOUNT_ID`), refuse to start on mismatch with a clear log line. Eliminates sub-account misconfig class. New env vars are documented in §2.2.
- **Periodic permission probe**: every 5 minutes, call a minimal spot read + a minimal futures read on each gateway. On unexpected permission errors, emit a critical anomaly (`api_permission_drift`). Rate-limit budget: 2 calls × N gateways × 12/hour = trivially below any venue's per-IP limit; no rate-limiter contention with the scanner.
- **Token-bucket rate limiter** per venue, with priority queues (orders + close paths > scans > diagnostics > housekeeping). Prevents scan storms from starving order placement.

**Process layer:**
- **Static-import smoke at deploy**: `python -c "import app.main"` must succeed before the container is considered healthy. Catches NameError / missing-import regressions before they kill cycles (§16 L02).
- **Cycle-error-rate anomaly**: §8.2 adds `cycle_error_rate_high` — fires when `error_count / cycles_in_window > 0.5`. Catches the silent-NameError class while it's measurable.
- **Graceful shutdown** on SIGTERM: cancel scans, complete in-flight orders' settlement reads (bounded to 10s — beyond that, persist as "in-flight unresolved" and let next-cycle reconciliation finish), flush DB, exit. Coolify deploys don't leave indeterminate state.
- **Migration unit tests**: every schema migration runs against a copy of the production DB in CI before deploy.

**Stablecoin / cross-stable:**
- **Tighter de-peg guard** (25 bps vs the previous 50). Reduces stablecoin-execution slippage at the small cost of more rejected swap candidates.

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
| **Sizing** (min/max position percent) | **Yes** | Strategies size differently. |
| **Execution** (sub-target sizing factor, perp leverage, max perp leverage, order policy) | **Yes** | Venue depth profile, slippage tolerance, and leverage policy diverge. |
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
| Basis dislocation exit bps | 50.0 | Mandatory exit when `(live_basis − entry_basis)` exceeds this — the live basis has moved farther adverse than our cost model assumed at entry, so the position's economics are no longer the ones the gate approved. |
| Min position percent | 0.005 (= 0.5%) | Sizing floor as fraction of equity. |
| Max position percent | 0.10 (= 10%) | Sizing ceiling. |
| Sub-target sizing factor | 0.75 | Final qty multiplier after closed-form max (§3.1 math). Absorbs reservation buffers, book-move slippage, and racing actors. Lower = lower reject rate at smaller position size. |
| Perp leverage | 1 | Only safe value for delta-neutral. |
| Max perp leverage | 1 | Hard cap (also shown on dashboard for effective-APY display). |
| Auto-transfer flag | true | Pre-trade spot↔futures rebalance for split-wallet venues. |
| Auto-quote-swap flag | true | Auto-swap USDT↔USDC pre-trade when a quote wallet is starved. |
| Futures-buffer percent | 0.20 (= 20%) | Margin buffer kept on futures wallet during post-cycle drain. |
| Tighter de-peg guard bps | 25.0 | Maximum |USDC/USDT − 1| basis tolerated for auto-swap (v1.4: reduced from 50 → 25 to bound execution slippage in stable swaps). |

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
- **Time-based exits (`max_hold_hours`).** Removed v1.3. Time is not a meaningful exit axis for this strategy — money is. Forward-profitability decay and basis dislocation cover every case the time cap was approximating, and they're directly economic rather than proxy. The column is retained on the schema per additive-only policy but the exit logic no longer consults it. See §16 Learning L37.
- **Limit-IOC orders + tick-buffer bps.** Removed v1.4. The iterative book-walk + limit-IOC + tick-buffer pattern was replaced by **closed-form sizing + market+FOK orders** (§3.1 math, §0.2). Tick-buffer fields (`entry_tick_buffer_bps`, `exit_tick_buffer_bps`) are retained on the schema but the order-placement path no longer uses them. See §16 L38–L40.

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
- The "naked spot" rendering path. Today there's special handling in `dashboard.html` for `status='naked_spot'` (suppress fake perp leg). A from-scratch rewrite MUST unify this into a generic "position has missing leg" component that handles both `naked_spot` (suppress perp leg) and `naked_perp` (suppress spot leg) through one code path keyed on status. Two parallel branches will drift; one component will not.

**Throw away:**

- Deprecated form fields still showing on `/safety` (`min_24h_quote_volume`, `min_order_book_depth_usdt`, etc. were purged from `/config` in PR #34 — but check `/safety` and `/monitoring` exports for stragglers). **v1.4 additions to the hide-list**: `entry_tick_buffer_bps`, `exit_tick_buffer_bps`, `max_hold_hours` — schema-retained per additive-only policy, but should not be editable from `/config` (the form should not even render their inputs). Add `sub_target_sizing_factor`, `basis_dislocation_exit_bps`, `depeg_guard_bps` as new inputs.
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

### 6.4 Hyperliquid (active — v1.5)
- **Auth model**: EVM. Operator provides `HYPERLIQUID_WALLET_ADDRESS` + `HYPERLIQUID_PRIVATE_KEY`. ccxt signs orders client-side; there is no API key/secret to rotate but the private key MUST be treated as a credential of equivalent sensitivity (§11 never-list applies).
- **Quote currency**: USDC only. Every spot pair is `BASE/USDC`; every perp is `BASE/USDC:USDC`. Same-stable arbs only at this venue today; cross-stable arbs require a second venue (Stage 3 §3.2).
- **Wallet structure**: single unified collateral pool. `consolidate_spot_wallets`, `transfer_spot_to_futures`, `transfer_futures_to_spot` are all no-ops. Account-mode probe declares `"unified"`.
- **Funding interval**: **1 hour**, NOT 4 or 8 hours. This dramatically reshapes the APY math: at hourly funding, periods/year = 8760 (vs 1095 at 8h). A modest 0.005% per-hour rate compounds to ~55% APY. The strategy SOP (§3.1) is unchanged; the math automatically picks up the venue's reported `interval_hours`. **Operators must understand this when calibrating `entry_min_net_apy` — a hyperliquid candidate's hot funding rate decays much faster than a CEX 8h one, so position holds are typically shorter.**
- **Order types**: native limit-IOC + market on the perp side; spot orders are also limit-IOC. The gateway uses `place_market_fok` for both, with the marketable-limit+FOK fallback pattern (the spec's standard escape hatch where a venue's market type doesn't accept FOK TIF).
- **Dust conversion**: no native endpoint. Sub-min positions are bought back / sold via normal market+FOK at the next opportunity; if perpetually unsellable they accumulate. The bot's phantom-recovery flow handles this — naked perps without spot counterparts get the buy-back path; naked spots below MIN_NOTIONAL stay as dust-class rows until the operator manually swaps them (or the value grows enough to trade out).
- **Capital flows (deposits / withdrawals)**: L1 transactions on Arbitrum. `external_id` = `hyperliquid:<flow_type>:<txhash>` (§7.5). Ingest pulls from the venue's user-fills + transfers history endpoints.
- **Position discovery on the venue**: `list_open_perp_positions()` reads from the user's open perp positions endpoint — straightforward unified call, no per-currency fetch.

### 6.5 Cross-stable USDT ↔ USDC
- Per-quote sizing reads the spot leg from the spot-quote wallet and the perp leg from the perp-quote wallet — these are independent buckets for cross-stable arbs.
- Auto-swap fires only for same-stable arbs (spot and perp share quote) when the relevant pool is below the min-notional and the other stable has surplus.
- Swap path: snapshot the USDC/USDT order book, run the closed-form sizing (§3.1 math), submit as **market+FOK** subject to the `depeg_guard_bps` (default 25 bps post-v1.4) limit. Cost is charged to the profitability gate before submission.

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
| `positions` | Lifecycle: `open → naked_spot | naked_perp → closed`. Carries entry prices, funding accruals, last close error. |
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
| `basis_dislocation_exit_bps` | float, default 50.0 | Mandatory-exit trigger: `(b_l − b_e) > this` → close. |
| `max_hold_hours` | integer, default 72 | **DEPRECATED** (v1.3). Column retained per additive-only policy (§7.3) but no longer read by the exit logic. Time-based exit was removed in favor of pure-economic exits (profitability + basis-dislocation). |
| `min_position_pct` | float, default 0.005 | Sizing floor as fraction of equity. |
| `max_position_pct` | float, default 0.10 | Sizing ceiling. |
| `entry_tick_buffer_bps` | float, default 1.0 | **DEPRECATED (v1.4)**. Was used to pad the limit price above worst-walked fill for limit-IOC entries. Order path is now market+FOK (§0.2); no limit price → no buffer needed. Column retained per additive-only policy. |
| `exit_tick_buffer_bps` | float, default 2.0 | **DEPRECATED (v1.4)**. Symmetric to above for exits. Column retained per additive-only policy. |
| `sub_target_sizing_factor` | float, default 0.75 | Final qty multiplier applied to the closed-form max from §3.1 math. Absorbs reservation buffers, book-move slippage, and racing actors. Lower = fewer rejects, smaller positions. |
| `depeg_guard_bps` | float, default 25.0 | Maximum \|USDC/USDT − 1\| basis tolerated for auto-swap (v1.4: tightened from 50 → 25). |
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
| `status` | string, indexed | `'open'` \| `'naked_spot'` \| `'naked_perp'` \| `'closed'`. Open-statuses tuple: `('open', 'naked_spot', 'naked_perp')`. Either naked status counts as "currently exposed". |
| `symbol` | string, indexed | Base asset (`BTC`, `ETH`). |
| `spot_symbol` | string | e.g. `BTC/USDT`. |
| `perp_symbol` | string | e.g. `BTC/USDT:USDT`. |
| `quote_currency` | string, indexed | Perp's quote (e.g. `USDT`). |
| `spot_quote_currency` | string, indexed | Spot's quote (differs from `quote_currency` for cross-stable arbs). |
| `quantity` | float | Base-asset qty. |
| `opened_at` | datetime | Set at first leg fill. |
| `entry_funding_rate` | float | At entry (decimal per window). |
| `last_funding_rate` | float | Refreshed every cycle while open. |
| `spot_entry_price` | float | Real for `open`; current-mid estimate for reconstructed `naked_spot`; `0` sentinel for `naked_perp` (no spot leg ever existed — UI must suppress display). |
| `perp_entry_price` | float | Real for `open`; `0` sentinel for `naked_spot` (no perp leg ever existed — UI must suppress display); current-mid estimate for reconstructed `naked_perp`. |
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
| `position_id` | integer, indexed, nullable | NULL for ghost-entries reconstructed during naked-leg recovery (either direction). |
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
                        ▼                       ┌────────────────┴────────────────┐
                  ┌──────────┐                  ▼                                 ▼
                  │   open   │            ┌────────────┐                    ┌────────────┐
                  └──────────┘            │ naked_spot │                    │ naked_perp │
                        │                 └────────────┘                    └────────────┘
                exit / close                    │                                  │
                        ▼                       │                                  │
                  ┌──────────┐  ◄───────────────┘                                  │
                  │  closed  │  ◄── hedge succeeds  /  sell-back  /  dust-convert  │
                  │          │  ◄── stale reconciliation (spot disappeared)        │
                  │          │  ◄────────────────────────────────────────────────  ┘
                  │          │       hedge succeeds  /  perp buy-back to flatten
                  │          │       stale reconciliation (perp short closed externally)
                  └──────────┘
```

`OPEN_STATUSES = ('open', 'naked_spot', 'naked_perp')` is used by every "currently exposed" query.

**Rendering rule for `naked_spot`**: the perp leg has `perp_entry_price = 0` (placeholder; no perp short was ever filled). The dashboard suppresses the perp-leg detail table for these rows — no fabricated entry/PnL numbers. Spot leg is real.

**Rendering rule for `naked_perp`** (symmetric): the spot leg has `spot_entry_price = 0` (placeholder; no spot buy was ever filled). The dashboard suppresses the spot-leg detail table for these rows. Perp leg is real. Generalisation: the UI's "missing-leg" treatment should be a single component keyed on status, not two parallel branches that can drift apart.

**Stale reconciliation**: at the top of every live cycle, any naked-spot position whose underlying spot balance is no longer in the wallet (sold externally, dust-converted by a prior cycle, Earn redemption, etc.) is auto-closed. Symmetrically: any naked-perp position whose underlying perp short is no longer open on the venue (closed externally, liquidated, expired) is also auto-closed in the same pass.

### 7.3 Migration policy

- **Additive only.** Never drop columns. Code may stop reading a column; the column stays.
- New columns get a sensible `DEFAULT`. Idempotent.
- One-shot value transforms gated by `config_schema_version` so they run exactly once per row regardless of restarts. The cursor lives on the global `strategy_config` row only — per-strategy rows inherit the global version.

### 7.4 Timestamp discipline

- Every datetime column persists **UTC-aware** values. Construct with `datetime.now(timezone.utc)`, never with `datetime.now()` (naive, local — corrupts every time-based filter on a non-UTC host).
- Every datetime emitted by `/api/diagnostics` is ISO-8601 with the `Z` suffix.
- Migrations that backfill datetime defaults (e.g. `'1970-01-01 00:00:00'`) MUST treat the sentinel as UTC.
- Comparisons (`utcnow() - ts`) require both sides to be aware; mixing aware + naive raises `TypeError` and breaks the cycle loop.

### 7.5 `capital_flows.external_id` construction

The column must be **UNIQUE** so re-ingesting venue history is idempotent. Construction rule (per venue, per flow type):

| Venue | Flow type | `external_id` source |
|---|---|---|
| Binance | Deposit | `binance:deposit:<txId>` |
| Binance | Withdrawal | `binance:withdrawal:<id>` (venue-side id; `txId` may be empty for in-flight) |
| Binance | Sub-transfer | `binance:subtransfer:<tranId>` |
| Binance | Universal transfer | NOT INGESTED — intra-account moves are not capital flows (see §6.1 / §10 cleanup) |
| KuCoin | Deposit | `kucoin:deposit:<id>` (history endpoint id, not txid) |
| KuCoin | Withdrawal | `kucoin:withdrawal:<id>` |
| KuCoin | Inter-sub-account transfer | `kucoin:subtransfer:<id>` |
| Hyperliquid | Deposit (Arbitrum L1) | `hyperliquid:deposit:<txhash>` |
| Hyperliquid | Withdrawal (Arbitrum L1) | `hyperliquid:withdrawal:<txhash>` |
| Hyperliquid | Internal transfer (sub-account) | `hyperliquid:transfer:<txhash>` |

The prefix `<venue>:<flow_type>:` guarantees disambiguation across venues even when raw ids collide. Auto-ingested rows without a sourceable external id are skipped (logged at INFO) — they're picked up the next time the venue history pages back through them.

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
    "by_status": { string: integer },            // e.g. {"open": 2, "naked_spot": 1, "naked_perp": 0, "closed": 14}
    "open":   [ OpenPosition ],                  // every Position with status == "open"
    "naked":  [ NakedPosition ]                  // every Position whose status is in ("naked_spot", "naked_perp"); each entry carries its own status so the consumer can tell the leg-direction apart
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
  "status":              "naked_spot" | "naked_perp",  // direction of the orphaned leg
  "symbol":              string,                 // base asset
  "spot_symbol":         string,                 // present for naked_spot; may be empty for naked_perp
  "perp_symbol":         string,                 // present for naked_perp; may be empty for naked_spot
  "quantity":            number,                 // base units; absolute magnitude regardless of direction
  "leg_entry_price":     number,                 // current-mid estimate for the surviving leg (spot price for naked_spot, perp mark for naked_perp)
  "notional_est":        number,                 // quantity × leg_entry_price
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
| `stale_naked_perp` | warn | A `naked_perp` Position older than 60min (symmetric to stale_naked_spot) |
| `no_trades_despite_scans` | warn | 0 recent trades AND > 20 rejections in window |
| `error_burst` | warn | > 20 ERROR events in window |
| `close_blocked` | warn | Open Position with non-empty `last_close_error` |
| `cycle_error_rate_high` | critical | `error_count / cycles_in_window > 0.10` (v1.4 — 10% errors-per-cycle; "almost everything broken" not "something wrong"). Catches silent-cycle-killer regressions (the L02 NameError class) by measuring the error-per-cycle ratio rather than just liveness. Operators can tune. |
| `api_permission_drift` | critical | The periodic permission probe (§3.1 mitigations) returned an unexpected permission error on any leg (v1.4). |
| `slippage_above_forecast` | warn | Realized fill on a leg diverged from the closed-form vwap forecast by more than `slippage_alert_bps` (default 10 bps). Sub-target sizing should keep this near zero; persistent alerts indicate book-snapshot staleness or matching-engine drift. |

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
| `below_min_pct_after_clamp` | Closed-form sizing × sub-target shrunk size below `min_position_pct × equity`. | None — genuinely too small for the binding wallet. |
| `no_book_depth` | The book snapshot returned zero (venue limit param, bad symbol, network). | Inspect the inner error. |
| `reservation_clamp_zeroed` | Wallet too small for even the smallest viable order at top-of-book. | None. |
| `basis_dislocated` | **DEPRECATED** — gate retired in PR #9. Should be 0. If non-zero, regression. |
| `spot_buy_error: ... Balance insufficient!` | **DEPRECATED in v1.4** — mid-fill reservation overflow class. Should be 0 post-v1.4 (market+FOK + sub-target sizing eliminates the class). If non-zero, regression. |
| `spot_buy_error: ... Order size below minimum` | Sizing floor below venue min after clamp + sub-target. Investigate sizing math; lower `min_position_pct` or raise equity. |
| `spot_ioc_zero_fill` / `perp_ioc_zero_fill` | **DEPRECATED in v1.4** — limit-IOC class. Market+FOK either fills or rejects atomically; replaced by `fok_rejected` below. |
| `fok_rejected_spot` / `fok_rejected_perp` | Market+FOK on a leg rejected by the venue (book didn't have full depth at submission time, or wallet reservation hit a buffer the sub-target factor didn't cover). Transient — retries next cycle with fresh snapshots. Persistent occurrence on a symbol → tighten `sub_target_sizing_factor`. |
| `single_leg_orphan` | Parallel placement filled one leg, the other rejected, and the same-cycle rollback succeeded. No naked row persisted. Should be rare; if frequent, indicates correlated leg failure (e.g. account-level rate-limit) that needs operator attention. |
| `strategy_disabled:<trade_type>` | Operator killed strategy via `/config`. | None unless unintentional. |

### 9.2 Common log patterns (informational)

- `Spot wallet consolidate <asset>: X main→trade` — KuCoin Classic sweep working.
- `Wallet snapshot <q> [Classic|UTA]·split|unified: spot free/total=...; fut free/total=...` — per-cycle wallet state.
- `Pre-trade rebalance skipped: <venue> reports unified margin` — PM/UTA correctly detected.
- `Pre-trade rebalance: X USDT spot→futures (equalize wallets so both legs can fund)` — Classic rebalance working.
- `Scan top <symbol>: predicted rate=X% per Yh → APY=Z%` — top-3 candidate diagnostic per cycle.
- `Phantom spot RESCUED into a hedged position` — phantom-recovery hedge succeeded; orphan spot is now a real hedged position.
- `Phantom spot CLOSED: sold ... → USDT` — phantom-recovery sell-back succeeded; orphan flattened.
- `Phantom perp RESCUED into a hedged position` — symmetric: naked-perp recovery hedge succeeded; matching spot bought.
- `Phantom perp CLOSED: bought back ...` — symmetric: naked-perp buy-back succeeded; short flattened.
- `Phantom dust detected: ... below venue min` — too small to sell, flagged for dust sweep.
- `Dust sweep CLOSED N naked_spot position(s)` — auto-conversion to BNB/KCS succeeded.
- `Stale naked_spot reconciled: <asset> no longer in spot wallet — marked closed` — stale cleanup fired.
- `Stale naked_perp reconciled: <symbol> short no longer open on venue — marked closed` — symmetric stale cleanup for naked perps (externally closed or liquidated).

### 9.3 Log patterns that indicate a regression

- `Loop iteration error (<mode>): name '<X>' is not defined` — Python NameError from a missing import. Open a PR. Past examples: `total_funding_income`, `rt_basis_bps`.
- `Reservation clamp on <symbol>` — should NOT appear post-v1.4 (closed-form sizing eliminates the walk-loop clamp class). If it surfaces, regression.
- `basis_dislocated` rejections — should be 0. Regression if non-zero.

---

## 10. Failure modes & recovery

| Failure | Detection | Recovery |
|---|---|---|
| Partial fill on the FIRST leg under an error response | Pre/post-balance snapshot delta in entry/exit path | Synthesize partial fill from the balance delta, continue to the SECOND leg at the smaller actual qty (PR #14). Applies to both directions: entry where spot partially fills before the order errors, and exit where the perp buy-back partially fills. |
| Naked spot left behind (spot filled, perp short failed) | Phantom-recovery sweep every live cycle (Phase A) | Try to hedge by shorting matching perp if profitable, else sell spot back to quote, else flag as dust |
| Naked perp left behind (perp short open, spot leg failed or sold) | Phantom-recovery sweep every live cycle (Phase A) — symmetric branch | Try to hedge by buying matching spot if profitable, else buy back perp to flatten. No "dust" branch — perp shorts have no native dust-conversion endpoint. |
| Dust below MIN_NOTIONAL (naked spot only) | Notional check in recovery | Attempt to convert to venue's native fee token (BNB / KCS) via the venue's dust-conversion endpoint. **If the endpoint exists but rejects the asset (e.g. DOGE ineligible on KuCoin)**, the position is marked `closed` with `last_close_error='dust_convert_ineligible: …'` and logged at WARN — accepting the sub-threshold loss as permanently unrecoverable. "Not available" (ccxt binding missing) is left for the next cycle without closing. |
| Naked spot whose underlying spot disappeared from wallet | Stale-reconciliation pass at top of recovery | Auto-mark closed |
| Naked perp whose underlying perp short was closed externally / liquidated | Stale-reconciliation pass at top of recovery — symmetric branch | Auto-mark closed |
| Wallet starvation | "below min position pct" rejection | Wallet-breakdown diagnostic surfaces where funds are stranded (`/api/diagnostics` payload includes the per-wallet-type view) |
| Book moves during round-trip | Market+FOK rejects atomically (no partial fill) | Reject, retry next cycle. Parallel placement (v1.4) shortens the round-trip; correlated rejections on both legs are a fair signal that conditions actually changed. |
| Single-leg orphan (parallel placement: one leg filled, other rejected) | Same-cycle reverse-market+FOK rollback on the filled leg | If rollback succeeds → reject with `single_leg_orphan`, no naked row. If rollback fails → naked_spot or naked_perp persisted; Phase A recovery handles next cycle (§3.1 Phase A, L21). |
| Realized slippage diverges from forecast | Per-leg `actual_avg_fill` vs `forecast_vwap`; tracked into the `slippage_above_forecast` anomaly (§8.2) | Persistent alerts → tighten `sub_target_sizing_factor` or shorten snapshot-to-submit window. |
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
- Closing an item from §18 → delete the row from §18 in the same PR that lands the resolution (the resolved behavior lives in its proper section now).

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
| 2026-05-16 | code | §2.2, web/app + loop/runner | **Loop runner — the v1.4 / v1.5 rewrite was NOT runnable until this PR.** The new `web/app.py` exposed the HTTP surface but never called `run_cycle` on a schedule — the rewrite was effectively a dormant binary. Added `loop/runner.py`: one background thread per `(mode, exchange)`, FastAPI `lifespan` startup/shutdown, graceful 10s SIGTERM drain, idempotent start, `BOT_WORKER_ENABLED=0` opt-out for API-only replicas, `ACTIVE_EXCHANGES` whitelist. Hyperliquid stays opt-in even with creds present (§6.4 reasoning). After this PR a single Coolify start-command swap (`uvicorn app.main:app` → `uvicorn web.app:app`) is the entire cutover. |
| 2026-05-14 | doc+code | §0.7, §2.2, §6.4 (new), §6.5 (renum), §7.5, §16 L11 | **v1.4 → v1.5: Hyperliquid added.** Third venue; DEX wallet model (EVM auth, single unified USDC pool, no sub-buckets, no transfers, no native dust endpoint). Funding settles HOURLY (interval=1h) — APY math automatically picks this up via the venue's reported interval. New env vars: `HYPERLIQUID_WALLET_ADDRESS`, `HYPERLIQUID_PRIVATE_KEY`, `HYPERLIQUID_EXPECTED_ACCOUNT_ID`. New reserved trade_type: `hyperliquid_same_venue_funding_arb`. `external_id` rule: `hyperliquid:<flow_type>:<txhash>` (Arbitrum L1). L11 expanded with HL quirks (hourly funding, EVM rotation = catastrophic, no dust endpoint). |
| 2026-05-13 | doc | §0.2, §3.1 SOP + math + new mitigation subsection, §4, §7.1.1, §8.2, §9.1, §10, §16 L38–L43 (new), §18 closures | **Execution-layer refactor (v1.3 → v1.4) — SPEC ONLY; code follows.** This PR rewrites the spec to describe the v1.4 execution layer. The implementation (bot.py, exchange.py, ccxt order parameters, sub-target sizing field on the schema, new anomalies, /config form changes, account-id env vars) is a follow-up. Monitor chat should NOT flag the running bot's v1.3 behavior as anomalous against this v1.4 spec until the code lands. (1) Iterative book-walk REPLACED by **closed-form sizing** (single pass per book, argmax of feasibility per level). (2) **limit-IOC DEPRECATED** in favor of **market+FOK** on every leg — pre-trade depth analysis + sub-target sizing makes market+FOK strictly better at our sizing range (L39). (3) Legs now fire **in parallel** (concurrent submission), not sequential — closes the inter-leg latency window that front-running actors exploited (L40). (4) Atomic spot+perp brackets preferred where the venue supports them. (5) New `sub_target_sizing_factor` (default 0.75) absorbs reservation buffers + book moves + racing actors (L41). (6) **Execution-risk mitigation policy** codified: deterministic client-order-ids, T-0 funding rate freshness, ccxt metadata cache TTL + reject-driven refresh, eager strategy-config seed, account-id assertion at boot, periodic permission probe, cycle-error-rate anomaly, static-import deploy smoke, graceful SIGTERM, token-bucket rate-limiter, migration unit tests, tighter 25bps depeg guard. (7) `entry/exit_tick_buffer_bps` deprecated (schema retained per additive-only). (8) Anomalies: `cycle_error_rate_high`, `api_permission_drift`, `slippage_above_forecast`. (9) Rejection categories updated: `fok_rejected_spot/perp`, `single_leg_orphan`; old `ioc_zero_fill` and mid-fill `spot_buy_error` classes deprecated. (10) §18 closures: per-symbol fee caching, two-hop step-1-ok/step-2-fail, wallet-consolidation atomicity, tick-buffer rationale. (11) L38–L43: walks are solvers, limit-IOC's price guarantee is illusory at our depth, sequential legs are a front-runner gift, sub-target is cheap insurance, co-temporal snapshots are non-negotiable, client-order-id is the cheapest network-blip insurance. |
| 2026-05-13 | doc | §0.3, §3.1 SOP + math, §4, §7.1.1, §16 L37 (new) | Exit-logic refactor (v1.2 → v1.3). **Removed `max_hold_hours`** time-based exit (column retained per additive-only policy; exit logic no longer reads it). **Added `basis_dislocation_exit_bps`** (default 50.0 bps) as a mandatory exit trigger: `(b_l − b_e) > threshold` → close. Rationale: the gate's worst-case cost is `m × \|b_e\|`; when the live basis has actually moved that far adverse, the position's economics no longer match what the gate approved. Pure-economic exits only — no time proxy. New L37: "Exit on economics, not on time." |
| 2026-05-13 | (merged-parallel) | §10 | Dust-convert ineligible fallback. `KuCoinGateway.convert_dust_to_native` now returns the actual API rejection error when a callable endpoint raises (rather than the misleading "not available" fallback). Dust-sweep in `recover_phantom_spot` marks positions `closed` with `last_close_error='dust_convert_ineligible: …'` when the endpoint exists but rejects the asset (e.g. DOGE). Resolves the DOGE $0.19 naked_spot stuck since 2026-05-11T19h. |
| 2026-05-12 | doc | §0.10, §3.1 SOP + math, §7.3/4/5, §18 (new) | Implementer-gap audit (v1.1 → v1.2). Added §0.10 stack assumptions (Python / FastAPI / ccxt / SQLAlchemy / UTC-aware datetimes). Math: defined "worst-walked price"; mandated tick-rounding + lot-step flooring with explicit conservative-rounding direction per side. SOP: detailed hedge-integrity detection rule, maintenance-mode mechanics (limit-IOC, never market), paper-mode probe-vs-order delineation. §7.3 cursor placement; new §7.4 timestamp discipline; new §7.5 `external_id` construction table. New §18 enumerates Tier B (strategy-correctness) and Tier C (polish) gaps that the spec is still silent on. |
| 2026-05-12 | doc | §0.3, §0.4, §3.1 SOP, §7.1.1, §7.2, §8.1, §8.2, §9.2, §10, §16 L03/L16/L21/L32 | Naked-direction symmetry audit. Added `naked_perp` as a first-class position status alongside `naked_spot`. Phantom-leg recovery sweep symmetrised across both directions. Dashboard rendering rule generalised: missing-leg suppression keyed on status. New `stale_naked_perp` anomaly + log patterns. `NakedPosition` JSON carries a `status` field + uses `leg_entry_price`. L21 explicitly direction-neutral. Concrete-example math cleaned up (no more "actually wait" in spec text). |
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
- **L03 — Display ≠ state.** Persisting a `naked_spot` position with `perp_entry_price=0` (or, symmetrically, a `naked_perp` with `spot_entry_price=0`) as a placeholder is fine as long as the UI knows it's a placeholder. Rendering it as a real "−$2.93 MTM" in the missing-leg card produced fabricated numbers the operator (correctly) called out. Display layer must know which fields are real vs sentinel, and the rule must apply identically to both directions.

### Money math

- **L04 — Signed economics, not absolute values.** Treating the entry basis as a cost in `abs()` form (matching the magnitude regardless of sign) is wrong: long-spot/short-perp benefits from positive basis on entry. The bot rejected its own bread-and-butter trades for months. Rule: always derive economic quantities from signed primitives; only take `abs()` when computing magnitudes for display.
- **L05 — Worst-case is conservative AND obvious.** The conservative round-trip basis cost is `−buffer × |entry|` regardless of sign, because the worst-case adverse exit is always "basis moves further positive". A formula that DIFFERS between signs (e.g. `−buffer × |entry|` for positive vs `+buffer × |entry|` for negative) is a sign-bug masquerading as conservatism. Code in this form has been wrong every time we've shipped it.
- **L06 — Funding interval matters more than funding rate.** APY = (1+r)^N where N depends on the funding interval. A "tiny" 0.62% rate at 4h compounds to 510,000% APY. The same 0.62% at 8h compounds to ~715%. Misreading the interval reads as a 700× error in APY.
- **L07 — Reservation ≠ avg-fill cost.** Limit-IOC orders reserve `qty × limit_price`, NOT `qty × mid_price`. Sizing `target_qty = sized_notional / mid` overflows the reservation on thin books → mid-fill "balance insufficient" with real partial-fill exposure. Always size against the limit price the order will actually carry.
- **L08 — One economic gate, not two.** A standalone basis sanity gate that runs BEFORE the profitability gate double-counts (the profitability gate already incorporates basis as a signed input). Single source of economic truth.
- **L09 — Heuristic liquidity gates lie; book walks don't.** Hardcoded thresholds like "min 24h volume", "min order-book depth at ±10 bps band" are crude approximations of "can my trade actually execute?". The real check is to walk the actual book at the actual sizing the bot will use. Heuristics rejected real opportunities AND passed real impossibilities.
- **L10 — Tier-1/2/3 separation pays for itself.** The funding scan can scan thousands of pairs cheaply; the book walk per candidate is expensive; the profitability gate after the walk is the real check. Combining tiers (e.g. always running the full check) burns API rate-limit. Splitting them with progressively-more-expensive checks is the right shape.

### Venue API patterns

- **L11 — Each venue lies differently; document every quirk inline.** KuCoin's book-depth API only accepts `limit=20` or `limit=100` (sliently rejects other values). Binance Futures balance is per-currency (calling without a currency returns USDT only). KuCoin Classic has 3+ spot wallets, only one of which the order book can spend. KuCoin's futures→spot drain needs the futures-side `transferOut`, NOT the spot-side universal-transfer. **Hyperliquid funding settles HOURLY** (interval_hours = 1) not 4h or 8h — failing to read the venue's reported interval mis-annualises by 4-8×. Hyperliquid auth is EVM signature (no api key / secret) so credential rotation = wallet rotation = on-chain transfer of the entire collateral pool to a new address; treat private-key compromise as catastrophic. Hyperliquid has NO native dust-conversion endpoint, so sub-min naked legs sit as dust rows until value grows enough to trade out. These aren't documented anywhere except this doc + the code that exercised them. **Every new venue quirk discovered in production goes here, immediately.**
- **L12 — Partial fills under error responses are real.** Some venues' "balance insufficient" responses come AFTER a partial fill has already occurred (matching engine matches what it can, then trips on remainder reservation). The HTTP error doesn't mean "nothing happened". Always re-read balance after an order exception; if quantity grew, the fill is real and must be reconciled.
- **L13 — Wallet abstraction lies.** A synthesised `spot.<asset>.free = trade + main` looks right but isn't: spot orders execute against `trade` only. Aggregating multiple sub-wallets into a single number hides which wallet the order book can actually spend. Either physically consolidate the wallets BEFORE every cycle (the chosen approach), or surface the per-wallet breakdown in the diagnostic; never present a single "free balance" derived from non-spendable sources.
- **L14 — Pseudo-tokens in spot balance responses.** Binance returns `LDUSDT`, `BFRBUSDT`, etc. in `fetch_balance` — these are Earn / Lending pseudo-tokens, not tradable. The bot tried to "recover" them as phantom spot positions on every cycle. Filter by known venue prefixes before the phantom-recovery loop.
- **L15 — String "0" is truthy in Python before float-parsing.** Binance's PM balance response returns balances as strings. An OR-chain like `r.get('crossMarginFree') or r.get('umWalletBalance')` short-circuits on the first truthy string — which includes the string `"0"`. The bot saw `$0.10` instead of `$30` for weeks. Rule: parse to numbers first, then combine; never trust truthiness of strings.

### State management

- **L16 — Phantom state is the enemy.** Any state that exists on the venue but has no row in the bot's DB is invisible to every reconciler, every gate, every monitor. The moment the bot detects an unexpected balance OR an unexpected open perp position (e.g., a partial spot fill the order error path didn't capture, or an externally-opened short), it MUST persist a `naked_spot` / `naked_perp` row immediately, before attempting any recovery. The portfolio view is broken otherwise. Both leg-directions need the same "persist-first, recover-second" discipline.
- **L17 — Stale reconciliation is mandatory.** A persisted position can outlive its underlying balance (operator sold externally, dust got swept, Earn got auto-redeemed). The bot must check at every cycle that DB state matches venue state for every open row, and auto-close the orphans. Without this, the dashboard shows positions that don't exist anymore.
- **L18 — Cache + force-refresh on every state mutation.** Balance fetches are cached for rate-limit reasons. After EVERY transfer, swap, or order placement, the cache must be invalidated explicitly or the next read returns pre-mutation data. The bug surface is "the bot says it has $9.90 but the venue says $0.10 — and the bot is using the stale value to make sizing decisions".
- **L19 — Idle-cycle gating.** Wallet rebalance is expensive (multiple API calls, multiple venue acks). On an idle cycle (no candidates passed the scan), rebalancing produces a drain↔rebalance oscillation that burns ~21k log events / day for zero benefit. Rebalance MUST be gated on "at least one candidate passed the scan AND needs both wallets funded".
- **L20 — Identical-error dedup.** A venue can return the same error repeatedly when a condition persists (e.g. genuinely insufficient balance for a transfer). The log layer must throttle identical-message errors per pattern; otherwise a single failing condition produces tens of thousands of events that drown the genuine signal.

### Recovery

- **L21 — Hedge before flat-close, in EITHER direction.** When recovering an orphan leg, the right first action is to try to hedge with the missing counterpart — if the forward profitability gate passes, the orphan becomes a real hedged position. Only fall back to flat-closing (selling the surviving spot, or buying back the surviving perp) when hedging isn't feasible (no listing, insufficient depth, gate fails). The recovery logic must be SYMMETRIC across the two naked directions; writing it for "naked spot" only is the brittleness that caused this learning. Reframed: the orphan is "a leg looking for its counterpart" — the counterpart is the matching short (for naked_spot) or matching long (for naked_perp). Same machinery, same gate, opposite trade.
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

- **L32 — Naked positions are first-class — both directions.** Any exposure on a venue must show on the portfolio view, regardless of which leg is the orphan. A naked spot (long, unhedged) and a naked perp (short, unhedged) are equally real, equally exposed to price moves, and equally must surface on the dashboard / transactions / diagnostics. Hiding either kind under "open positions only with a DB row" makes the operator's mental model inconsistent with the actual account balance. Operator's quote: "Any position is the position!" Recovery, accounting, and UI must treat the two directions as siblings, not as a primary plus an afterthought.
- **L33 — Show the math the gate did.** Rejection log lines must show the comparison the gate actually performed. A line saying "net 11.57% < 10%" is fine; a line saying "11.57% < 10%" without "net" looks like a bug because the math doesn't check out at first glance.
- **L34 — Per-position thresholds in per-position rows.** Once thresholds are per-strategy, the dashboard's open-positions table should show each row's strategy threshold inline. Showing a single global threshold in the summary card is correct (it's the default); showing it on a per-position row creates a wrong impression.

### Process

- **L35 — Audit-pass discipline.** Every doc rewrite goes through at least two audit passes. Round 1: full draft. Round 2: cross-check every claim against the actual code. Round 3 (optional): readability + cross-reference consistency. In this session, the round-2 audit caught a NameError regression (`rt_basis_bps`) AND three "active config fields" that were in the doc but never actually read by the bot. Doc audits find code bugs.
- **L36 — Operator vocabulary > internal naming.** Page names ("Dashboard", "Configuration", "Safety & Rules") match how the operator thinks about the bot. Field names should too. When the developer term and the operator term diverge, the operator term wins on UI surfaces; the developer term can stay in the schema for compatibility.

### Exit logic

- **L37 — Exit on economics, not on time.** A max-hold timer is a proxy for "the operator doesn't trust the economic gates to fire on time." The right fix is to make the economic gates trustworthy, not to add a fallback time cap. The bot now exits voluntarily when forward net APY drops below the exit threshold (deferrable on adverse basis) and mandatorily when the live basis dislocates beyond `(b_l − b_e) > basis_dislocation_exit_bps` — directly economic, no time proxy needed. A position whose funding stays high and basis stays sane can run for weeks; one whose economics break exits within a cycle. Removed `max_hold_hours` in v1.3.

### Execution

- **L38 — Iterative walks are solvers, not strategies.** A 4-pass walk loop with `target_qty` shrinking each pass is a fixed-point iteration on a problem that has a closed-form solution. For a monotone constraint set (cumulative depth vs reservation-per-level), the optimum is a single binding level per book, computable in one pass. Iterative solvers exist when you don't yet understand the problem; once you do, replace them with the closed form. Replaced the walk in v1.4 — same answer, no race between passes, materially faster cycle.
- **L39 — Limit-IOC's price guarantee is illusory at our sizing range.** The pre-v1.4 spec used limit-IOC for the price guarantee. But on the books we trade (thin enough that we walk multiple levels), the tick-buffer we have to add to make limit-IOC actually fill (1–2 bps) is ≈ market-order slippage at the same depth. The "guarantee" is a paper one: you pay it as buffer instead of as slippage, but the actual fill quality is the same. Meanwhile, limit-IOC's round-trip (walk → construct limit → submit) gives other actors a latency window market+FOK closes. Net: market+FOK is strictly better for our sizing range.
- **L40 — Sequential legs are a front-runner gift.** Spot-then-perp ordering creates a ~50-200ms gap between leg-1 print and leg-2 submission. That gap is visible to anyone watching the order flow. Any actor who knows the strategy can move the perp's price against you between when spot prints and when perp lands. Parallel placement (concurrent async + atomic brackets where available) shrinks this gap to the venue's own internal coordination time — measured in microseconds, not the network round-trip. Adopted in v1.4.
- **L41 — Sub-target sizing is cheap insurance.** Sizing at 99% of the closed-form max chases the last 1% of position at the cost of a non-trivial reject rate from reservation buffers, racing actors, and book moves. Sizing at 75% buys near-zero reject rate for a 25% position-size haircut. Across a long-run portfolio compounded daily, the "missed 25%" on each trade is well dominated by the "every-trade clean-fill" property. Adopted as policy in v1.4 with `sub_target_sizing_factor = 0.75`.
- **L42 — Co-temporal book snapshots are non-negotiable.** Closed-form sizing math is only correct if both books were snapshotted at the same instant. A 100ms stale snapshot on the spot side while the perp side is fresh produces a forecast that diverges from reality on submission. Use websocket frames where available; bound REST snapshots to <50ms between calls and reject the candidate if the window blows out. Adopted in v1.4.
- **L43 — Client-order-id is the cheapest insurance against network blips.** A deterministic order id (e.g. `hash(cycle_id, candidate_symbol, leg)`) means a retry of the same call is a no-op at the venue — duplicate-order rejects rather than placing two orders by accident. The bookkeeping is one extra field; the alternative is "the network blipped, did my order go through or not?" forever. Adopted as policy in v1.4.

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
1. Implement the venue gateway protocol: read funding rates, snapshot book (spot + perp), place market+FOK on each leg (idempotent client-order-id, parallel-fire), transfer (spot↔futures), consolidate wallets, dust-convert, account-mode probe.
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
5. Position state machine: `open ↔ {naked_spot, naked_perp} ↔ closed`, transitions per §7.2. Both naked directions are first-class states; no special-casing only one.
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
- HTML pages render correctly with naked-spot AND naked-perp rows, multi-strategy, and various edge-case states present.

#### Stage 5 — Live mode validation (paper-only first, then dry-run)

**Entry criteria:** Stage 4 done.

**Work:**
1. Deploy new code in PAPER mode only. Run alongside the production bot (read-only on live wallets — paper sends no real orders).
2. Compare paper-mode decisions across the two bots for 48 hours. Differences must be intentional (e.g. fixed bugs in the new code) or zero.
3. After 48h clean: enable live mode on a single venue, single strategy. Monitor closely.
4. After 48h clean live: enable the second venue.

**Exit criteria:**
- 48h paper-mode parity (or all deltas intentional + documented).
- 48h live single-venue clean: no naked positions (either direction) accumulated, no error bursts, all trades reconcile.
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

## 18. Open implementation gaps (for the next audit pass)

Catalogued from the "if a from-scratch coding agent built this with only the spec, where would it mess up?" review (2026-05-12). Tier A items have been closed inline above; Tier B and C remain TODOs. Each item is a place where the spec is silent or hand-wavy enough that two different implementers would build incompatible behaviors.

### Tier B — strategy correctness gaps (resolve before the rewrite)

| Gap | Where in spec | Resolution direction |
|---|---|---|
| **Funding accrual formula not written** | §0.5 "Funding income" defines the concept; §7.1.1 `funding_income_accrued` + `last_funding_accrual_ts` columns exist. Update rule per cycle is not specified. | Paper: `Δaccrued = position_notional × current_funding_rate × min(1, elapsed_since_last_accrual / funding_interval)`. Live: derive from per-cycle delta of the futures wallet's funding-history endpoint, attributed by symbol. Document both. |
| **Cross-stable arb sizing formula** | §6.5 says "independent buckets" but never writes the formula. | `sized_qty = min(spot_quote_wallet × safety_factor / spot_limit, perp_quote_wallet × safety_factor × leverage / perp_limit)` then floor to lot step. Same formula as same-stable but reads each wallet independently. |
| **Auto-swap 5 bps basis cost** | §3.1 math `fees_RT_bps` formula uses "+5 bps USDC/USDT spot basis" as a magic constant. | Promote to a per-strategy config field with default 5.0; or document that it's a hardcoded safety margin and why 5 specifically. |
| ~~**Per-symbol fee caching policy**~~ | RESOLVED v1.4 in §3.1 mitigation policy: per-cycle TTL with force-refresh on tick/lot/min-notional rejects. | — |
| ~~**Two-hop drain: step-1-succeeded, step-2-failed**~~ | RESOLVED v1.4 in §3.1 mitigation policy: best-effort, partial sweep is still useful; next cycle picks up stranded funds. | — |
| ~~**Wallet-consolidation atomicity**~~ | RESOLVED v1.4 in §3.1 mitigation policy: best-effort per source bucket; per-bucket failure logs WARN, continues. | — |
| **Mode-state vs strategy-state precedence** | §4 + §7.1 define both `mode_state.entry_enabled` and `strategy_state.entry_enabled` but never the AND. | AND of both. Rejection category when mode-disabled: `mode_disabled:<mode>`. When strategy-disabled: `strategy_disabled:<trade_type>`. Document the precedence. |
| **Partial-window funding on fresh opens** | §0.4 + §3.1 — position opens mid-window; first funding payment lands at next scheduled time. | The funding accrual formula above (with `min(1, elapsed/interval)`) handles this correctly; document the property explicitly. |

### Tier C — polish / future cleanup

| Gap | Where in spec |
|---|---|
| Vocabulary drift: "asset" vs "currency", "60min" vs "3600s", "spot symbol" vs "spot pair" | scattered |
| ~~Tick-buffer rationale~~ | RESOLVED v1.4: fields deprecated; market+FOK has no limit price to buffer. |
| `rejected_candidates` retention policy ("prune old rows") — keep how long? | §3.1 SOP post-cycle |
| `/config` strategy tab source — `StrategyConfigPerStrategy` rows, or a static `ACTIVE_STRATEGIES` constant? | §5.3 |
| Dashboard view-cookie name + max-age | §5 |
| Should `/api/diagnostics` omit `perp_entry_price=0` / `spot_entry_price=0` sentinel fields for naked rows, or surface them with a `is_real_leg` boolean? | §8.1 |
| Re-validate the "venue dust endpoint min" floor: $0.10 tracking floor is a magic number | §3.1 SOP Phase A |
| `last_close_error` retention — does it clear on successful retry, or accumulate? | §7.1.1 |

Resolution policy: each Tier B gap should be closed in a separate doc PR (or in the PR that implements the corresponding behavior). Tier C gets one consolidated cleanup PR before the rewrite kicks off at Stage 0.

---

> **For the monitor chat:** Always read this doc from the latest `main` before judging anomalies. The definitions (§0), strategy SOP + math (§3), rejection categories (§9), failure modes (§10), response policy (§11), and learnings (§16) are your operating manual. When a new pattern emerges in production, add a learning to §16 in the same PR that ships the fix. If a rewrite is in progress (§17), be aware that the new code may behave differently from the old in documented ways — check the changelog before flagging differences as anomalies.
