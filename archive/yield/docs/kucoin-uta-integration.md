# KuCoin Unified Trading Account (UTA) integration

Roadmap doc — implementation deferred until the user enables UTA on the
sub-account. Mirrors `binance-pm-bfusd-integration.md` so both venues
operate on a shared "unified margin pool" model going forward.

## What changes

KuCoin's classic account splits cash across three isolated wallets:

* **Main / Funding** — deposits land here; the bot uses it as the "Earn"
  surface (KuCoin auto-lend draws from this wallet)
* **Trade** — spot orders execute here
* **Contract** — USDM-perp futures margin lives here

The bot today shuttles USDT between these wallets via inner-transfer
(main↔trade) and universal-transfer (trade↔contract), and reads each
wallet separately. This is operationally fine but loses the
cross-collateralisation benefit Binance PM gives us: under PM idle USDT
becomes BFUSD which simultaneously earns yield AND posts margin.

KuCoin's **Unified Trading Account (UTA)** unifies spot, margin, and
futures into a single collateral pool. UTA accounts can hold multiple
quote assets (USDC, USDT, USDX) as cross-collateral, and the futures
liquidation engine evaluates net portfolio risk rather than isolated
contract margin. Yield: KuCoin's auto-lend / Pool-X integration with UTA
exists but is more limited than Binance BFUSD — typical yields on USDC
auto-lend hover around 3–8% APY (variable, market-driven).

## What we'd build

### 1. Account-mode detection

* Reuse the live `account_type()` probe already added on
  `KuCoinGateway`. UTA returns `(label='Unified Trading Account
  (UTA)', detail='UTA · <mode>')` so /monitoring shows the operator's
  current account state.
* On gateway init, read the account type once and stash it as
  `self._is_uta`. Method overrides switch on this flag.

### 2. Endpoint routing

| Operation | Classic (current) | UTA |
|---|---|---|
| Balance read | `fetch_balance({type:trade/main/contract})` × 3 | `utaPrivateGetAccountBalance` (single call) |
| Spot order | `create_order` on `kucoin` client | `utaPrivatePostOrder` (cross-margin spot) |
| Perp order | `create_order` on `kucoinfutures` client | `utaPrivatePostOrder` (with `category=linear`) |
| Open positions | `fetch_positions()` on futures client | `utaPrivateGetAccountModePositionOpenList` |
| Spot↔futures transfer | inner-transfer / universal-transfer | no-op (UTA unifies) |
| Set leverage | `set_leverage(N, symbol)` on futures client | `utaPrivatePostAccountModeAccountSetLeverage` (per-symbol still required) |
| Earn surface | main wallet balance | UTA's auto-lend USDC/USDT line item |

ccxt 4.x already exposes `utaPrivate*` methods. The order-routing layer
(`_market_order`) needs venue-aware branching, mirroring the Binance PM
pattern.

### 3. Idle-cash routing

Under UTA, idle USDT auto-lends if the user enables auto-lend per asset
in the KuCoin UI (same setup as Binance auto-collection for BFUSD). The
bot's earn-sweep becomes a no-op on KuCoin under UTA — auto-lend handles
it. `earn_balance_usdt` reads the auto-lent USDT line item from the
unified balance instead of querying the main wallet directly.

### 4. Liquidation buffer

UTA evaluates futures liquidation against net portfolio equity (cross-
collateralised), not the contract wallet alone. The bot's
`futures_buffer_pct` becomes a *minimum portfolio buffer* under UTA —
practically the same setting, just measured against unified equity
rather than contract-wallet equity.

### 5. Code structure

* `app/exchange.py` — extend `KuCoinGateway` with UTA-aware overrides.
  Most methods inherit from the existing classic implementation and
  guard with `if self._is_uta:` to swap the endpoint. Single class, no
  inheritance hierarchy needed.
* `app/config.py` — no new config; `binance_max_bfusd_pct` already caps
  yield-bearing collateral; we'd add `kucoin_max_lend_pct` (default 20%)
  with the same shape if needed.
* `docs/kucoin-uta-integration.md` — this doc.

### 6. Risks / open questions

* **UTA is opt-in per account.** The user has to flip it on in the
  KuCoin UI before the bot's UTA endpoints work. The `account_type()`
  probe gates this — if it returns Classic, the gateway falls back to
  the existing classic methods. Same fail-soft pattern as Binance.
* **UTA yields are lower than BFUSD.** ~3–8% vs ~6–25% on Binance PM.
  Acceptable for "yield + margin in one product" but won't be the
  highest-yield venue for idle USDT. The cross-venue orchestrator
  (future trade type) will route capital toward Binance PM by
  preference once we measure realised yields side-by-side.
* **Currency support varies.** UTA initially supports USDT, USDC, BTC,
  ETH as collateral; meme/altcoin spot legs may not auto-collateralise
  the same way. Per-symbol acceptance check on entry.
* **API surface drift.** `utaPrivate*` methods are newer in ccxt and
  occasionally rename between versions. The fail-soft probe pattern
  (`getattr(...) or fall_back`) used for Binance PM applies here too.

## Phasing

* **Phase 1:** account-type probe (already shipped — `account_type()`
  surfaces "UTA" or "Classic" on /monitoring).
* **Phase 2:** read-only UTA balance — gateway's `_fetch_balances_uncached`
  routes to `utaPrivateGetAccountBalance` when `_is_uta=True`. No
  order-routing changes; user can verify the unified-pool number on the
  dashboard before flipping the order path.
* **Phase 3:** flip orders to `utaPrivatePostOrder`. Risky-most change;
  test in paper mode by mocking the UTA response shape.
* **Phase 4:** drop the inner-transfer / universal-transfer plumbing on
  KuCoin under UTA (no-op). Simplifies provision_margin further —
  cross-collateral handles the buffer math.

## Estimated effort

Roughly the same as the Binance PM refactor — ~150–200 lines, single
focused PR. Lower-risk than Binance because the user can revert to
Classic in the KuCoin UI (instant), and the bot fails-soft to
Classic endpoints if UTA detection fails.

## Decision

Hold this until the user enables UTA on the KuCoin sub-account. The
classic isolated-wallet design works correctly today; UTA is a
yield-and-simplification upgrade, not a correctness fix.
