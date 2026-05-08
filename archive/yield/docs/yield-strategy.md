# Yield strategy roadmap

We have two distinct yield-optimisation problems to solve at different
phases. Naming them explicitly so we don't conflate them in code or
discussion.

---

## Phase 1 (current) — collateral-yield optimisation

**Definition:** capital that *must* serve as margin / collateral for
open positions or to back imminent entries — earn yield on it WITHOUT
withdrawing it from the collateral pool.

**Constraints:**
- Asset must count toward cross-margin collateral on the venue.
- Must be redeemable on demand (or at least counted at par by the
  venue's liquidation engine).
- Should not introduce locked-up periods that conflict with trade
  margin needs.

**Per-venue surfaces we use:**

| Venue | Asset | Mechanism | API path | Rate (rough) |
|---|---|---|---|---|
| Binance PM | **BFUSD** | Subscribe USDT → BFUSD via Simple Earn flexible (post Aug-2025 migration). BFUSD counts as PM cross-collateral with 100% ratio in Multi-Asset Mode. | `POST /sapi/v1/simple-earn/flexible/subscribe` with BFUSD productId | 12-35% base, 15-47% boosted-as-collateral |
| KuCoin UTA | **USDT (auto-lent)** | Auto-Lend toggle on the unified pool; lent USDT remains cross-margin collateral. | `POST /api/v1/margin/toggle-auto-lend` (one-shot per startup; lending is then continuous) | 3-8% APY (variable) |
| KuCoin Classic | **USDT in main wallet (auto-lent)** | Same auto-lend endpoint; bot moves idle USDT to main wallet which is the auto-lend source. | Same as UTA | 3-8% APY |
| Onchain (future) | **sUSDe / USDC.e** | Hyperliquid / Drift accept yield-bearing stables as collateral natively. | Per-protocol contract calls | 5-15% APY |
| IBKR (future) | **Cash sweep on idle USD** | Brokerage cash sweep (~4-5% APY on USD on Pro accounts) | n/a — automatic on the account | ~4-5% |

**What we explicitly DON'T touch in Phase 1:**

- **Binance Simple Earn flexible USDT (LDUSDT)** — works as collateral
  but yield is far below BFUSD (~1.5% vs ~25%). We use BFUSD instead.
  LDUSDT becomes interesting only if BFUSD yield drops below LDUSDT or
  BFUSD enrolment is blocked.
- **KuCoin Simple Earn / Pool-X** — DOES NOT count as UTA collateral.
  Subscribing would reduce available margin. Skip entirely.
- **Lock-up / dual-asset products** on either venue — capital becomes
  unavailable for trades. Skip entirely.

**Cap on yield-bearing share** (from `cfg.binance_max_bfusd_pct`,
default 0.20) — keeps a portion in plain USDT for instant deploy
without redemption-queue tail risk. Bumpable to 0.80+ once redeem
behaviour is observed under live load.

---

## Phase 2 (future) — idle-yield optimisation on excess capital

**Definition:** capital that's *not currently needed* as collateral —
the portion above the largest expected position-opening cost plus a
safety multiple. Sweep the excess into the highest-yielding venue,
even if that means it's NOT usable as immediate collateral.

**When this becomes relevant:**
- Pool grows large enough that the marginal collateral need is small
  relative to total equity (e.g. only 30% of capital is collateralising
  open positions at any time → 70% is genuinely idle).
- Cross-venue routing: capital sitting on Venue A but Venue B has
  better funding opportunities → move it.

**Candidate destinations:**
- **Binance Simple Earn flexible** at the headline rate (no BFUSD
  cap), or fixed-term locked positions if we know the capital won't
  be needed for N days.
- **KuCoin Simple Earn / Pool-X** with longer terms.
- **Onchain DeFi** — sUSDe, sDAI, Aave USDC supply, Spark, etc.
  Higher yields but bridge / smart-contract risk.
- **IBKR money-market funds** for fiat balances.
- **Sub-account-to-master sweep** so master earns on Binance earn at
  the higher tier (master accounts often have higher Simple Earn
  caps than sub-accounts).

**Required infrastructure (not built yet):**
1. **Free-capital tracker** — distinguish working capital (margin +
   buffer) from genuinely idle capital. Inputs: current open notional,
   recent fill history (for sizing the next entry), `cfg.futures_buffer_pct`.
2. **Multi-product yield comparator** — fetch live APY across each
   venue's products at a configurable cadence (e.g. hourly).
3. **Sweep orchestrator** — when free-capital × estimated APY-delta
   exceeds a threshold (gas / transfer-fee adjusted), execute the move.
4. **Recall logic** — when a high-APY entry opportunity appears that
   needs more capital than the working pool, fast-redeem the swept
   excess and route it.
5. **Cross-venue transfers** — internal-transfer history is now wired
   on Binance (Universal Transfer log) and KuCoin (universal-transfer
   API). Moving funds between sub-accounts of the same exchange is
   cheap; cross-exchange withdrawals incur on-chain fees + latency.

**Risk considerations** that don't apply to Phase 1:
- **Rebalancing slippage** — sweeping in/out has friction.
- **Locked-term yield products** — committed for N days; need either
  early-redemption fees or never lock more than the absolute idle
  portion that's certain to stay idle.
- **De-pegging / smart-contract risk on DeFi sources** — much bigger
  than the centralised yield products in Phase 1.

---

## Decision: ship Phase 1 fully before touching Phase 2

Phase 1 is "free money" — the capital is already there serving as
collateral; we just need to route it to a yield-bearing wrapper. No
additional risk relative to holding plain USDT.

Phase 2 introduces real strategy decisions: how much is "excess",
which products at which yields, when to redeem, etc. It's a product
decision, not a code decision, and should wait until:
- Phase 1 is running cleanly for ≥30 days
- We have observed actual collateral-utilisation patterns (what
  fraction of equity is genuinely idle?)
- BFUSD / auto-lend yields are baselined so we can quantify the
  delta from sweeping to alternative products

**Code seam where Phase 2 plugs in:** `app/bot.py` → after the
post-trade earn sweep, add an `_idle_yield_sweep()` step that reads
`cfg.idle_yield_threshold_usdt` and routes anything above it to the
configured high-yield destination. Same per-venue gateway abstraction.
