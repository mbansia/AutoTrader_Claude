# Binance Portfolio Margin + BFUSD integration

Roadmap doc — not implemented yet.

## What changes

Today we run on Binance's **Classic** account (separate spot wallet, separate
USDM-futures wallet, separate Simple Earn). Idle USDT either earns interest in
Simple Earn (~5% APY flexible) **or** posts margin in the futures wallet — never
both. So with the 20% futures buffer we keep, ~20% of every position's notional
sits in cash earning nothing.

Switching the account to **Portfolio Margin (PM)** unlocks **BFUSD** — Binance's
yield-bearing margin asset — as collateral for USDM futures. BFUSD:

* Is pegged 1:1 to USDT, redeemable any time (no lockup).
* Distributes daily airdrops + boosted APY to PM users. Quoted yield ranges:
  * **Tier 1 (basic PM users):** ~6–10% APY in airdrops + base yield.
  * **Premium tiers ($250+ BFUSD held):** ~15–25% APY (higher in volatile
    markets, has hit 30%+ historically).
  * Yields fluctuate with funding-rate environment; treat as variable.
* Counts as initial-margin and maintenance-margin collateral on all USDM
  perps under PM (Binance discounts it slightly vs USDT — typically 95–98%
  collateral value).

Net effect for our bot: idle USDT auto-converted to BFUSD earns yield AND
posts margin simultaneously. The 20% futures buffer goes from
"opportunity-cost dead weight" to "yield-bearing safety margin."

## What we need to build

### 1. Detection + opt-in

* New env flag `BINANCE_PM_ENABLED` (default off — Classic remains the
  fall-through path). When set, the gateway routes orders / balances /
  transfers through the PM API instead of Classic.
* On startup, probe `GET /papi/v1/account` to confirm the account is in PM
  mode; surface a clear error if the env is set but the account isn't
  actually PM.

### 2. New gateway class — `BinancePortfolioMarginGateway`

Inherits from `BinanceGateway` but overrides:

| Method | Classic path | PM path |
|---|---|---|
| `safe_balances` | `/api/v3/account` + `/fapi/v2/balance` | `/papi/v1/balance` (unified across spot+futures+earn) |
| `_market_order` (perp) | `/fapi/v1/order` | `/papi/v1/um/order` |
| `_market_order` (spot) | `/api/v3/order` | `/papi/v1/order` |
| `transfer_spot_to_futures` | `/sapi/v1/asset/transfer` | no-op (PM unifies) |
| `earn_balance_usdt` | Simple Earn flexible position | sum of BFUSD wallet + LDUSDT |
| `earn_subscribe` | Simple Earn subscribe | BFUSD purchase via `/papi/v1/auto-collection` or BFUSD subscribe endpoint |
| `earn_redeem` | Simple Earn redeem | BFUSD redeem |
| `set_margin_mode_and_leverage` | per-symbol cross/iso | PM is unified-margin only — cross is implicit, leverage still configurable |

### 3. Auto-collection setup

PM offers an auto-collect feature: when futures equity drops below a threshold,
margin is automatically pulled from the unified margin pool (BFUSD + spot).
We call `POST /papi/v1/auto-collection` once at startup to enable it. Removes
the need for `provision_margin` to manually shuttle cash.

### 4. Idle-cash routing changes

Current earn-sweep moves USDT from spot wallet into Simple Earn. Under PM:

* All USDT (regardless of source — deposit, position close, funding payment)
  flows into the unified margin pool.
* Sweep instead converts spot USDT → BFUSD (subscribe).
* Pre-trade redemption is unnecessary: BFUSD itself is collateral, no
  conversion needed before opening a position.
* On withdrawal: bot redeems BFUSD → USDT first.

### 5. Liquidation buffer

PM uses cross-margin across the entire account (spot + BFUSD + futures positions).
Maintenance margin is computed on net portfolio risk. The `futures_buffer_pct`
config field becomes a *minimum portfolio buffer* instead of a per-perp buffer
— much safer because it pools risk across symbols.

Recommendation: keep 20% as default but the hedged nature means a 20% adverse
move on the short would be partially offset by the spot leg appreciating, so
the practical buffer-need is lower. Could drop to 10% under PM safely.

### 6. Code structure

* `app/exchange.py` — add `BinancePortfolioMarginGateway`. Minimal new code
  because most methods inherit from `BinanceGateway` and just swap the API
  prefix in a helper.
* `app/bot.py` — `make_gateways()` picks PM vs Classic based on the env flag.
  No other change in the cycle loop — it's still one venue, just talks to
  different endpoints.
* `app/config.py` — `binance_pm_enabled: bool = False`.
* `docs/binance-pm-bfusd-integration.md` — this doc.

### 7. Risks / open questions

* **PM order mins are different.** PM's USDM-futures min order is typically
  $5 (vs $5 on Classic) but has occasionally been $10 — verify on each
  symbol via `/papi/v1/um/exchangeInfo`.
* **BFUSD APY is variable and unguaranteed.** Cap exposure based on what's
  comfortable; add a config field `binance_max_bfusd_pct` (default 80%) to
  cap the share of equity converted to BFUSD.
* **Redemption queue.** BFUSD redemption is *usually* instant but can queue
  during heavy market stress. If we need cash to top up an unhedged position,
  the queue is a tail risk — keep a small USDT float (~5% of equity) as
  immediate-deploy reserve.
* **Sub-account limitations.** Some PM features are gated behind the
  master account; sub-accounts need explicit enablement (which the user
  has confirmed). Worth re-verifying the BFUSD subscribe endpoint is
  callable from the sub key before we ship.
* **Reverting from PM.** If the user ever switches the account back to
  Classic, BFUSD positions auto-redeem to USDT but pending PM-routed
  orders may behave unexpectedly. Add a startup check that if PM is
  configured but the account is Classic, we abort with a clear error.

## Estimated effort

* `BinancePortfolioMarginGateway` skeleton + balance/earn methods: ~80 lines.
* Order routing override: ~50 lines.
* Auto-collection bootstrap + startup probe: ~30 lines.
* Tests + dashboard polish (BFUSD APY display, "Auto-collected" log lines):
  ~40 lines.

Total: ~200 lines, single PR. Should be feasible in one focused session.

## Phasing

* **Phase 1:** read-only — gateway class fetches PM balance, surfaces BFUSD
  + LDUSDT in the equity composition. No order routing changes; user can
  see what PM looks like before flipping the order path.
* **Phase 2:** flip orders to PM endpoints. Most invasive change. Test in
  paper mode first by mocking the PM responses.
* **Phase 3:** earn-routing — swap Simple Earn calls for BFUSD subscribe /
  redeem. Earn-sweep keeps idle in BFUSD.
* **Phase 4:** drop the futures-buffer math (PM cross handles it),
  simplify `provision_margin`.

Ship phases independently so each is reversible.
