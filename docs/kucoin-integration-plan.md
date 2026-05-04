# KuCoin Integration Plan

This doc lays out the approach for adding KuCoin alongside Binance. It is a
plan — no code yet — so we can agree on shape before building.

## 1. Account setup

You'll need:

1. **KuCoin sub-account** (Account Center → Sub-Accounts → Create). The arb bot
   should never run on the master account; sub-accounts isolate API blast
   radius and let you cap balance via internal transfers.
2. **API key on the sub-account** with these scopes ticked:
   - **General** (read account info, balances, orders).
   - **Trade** (place/cancel orders on spot + futures).
   - **Transfer** (universal transfer between spot/futures/earn).
   - **Earn** if/when KuCoin exposes the SAPI for it (see §3 below).
3. **API passphrase** — KuCoin requires a third secret (alongside key +
   secret), set when you create the key. Treat it like a password.
4. **IP whitelist** — same workflow as Binance. The Coolify outbound IP shown
   on Safety & Rules works for both.

## 2. KuCoin's product surface (mapped to our needs)

| Concern                        | Binance                                                                | KuCoin                                                                       |
| ------------------------------ | ---------------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| Spot trading                   | `binance` exchange in ccxt                                             | `kucoin` exchange in ccxt                                                    |
| Perp/futures trading           | `binanceusdm` (USDM-margined linear)                                   | `kucoinfutures` (USDT-margined linear, e.g. `XBTUSDTM`, `ETHUSDTM`)          |
| Funding rate fetch             | `fetchFundingRates()`                                                  | `fetchFundingRates()` — same ccxt unified call                               |
| Order book depth               | `fetchOrderBook(symbol)`                                               | `fetchOrderBook(symbol)`                                                     |
| Universal transfer spot↔fut    | `POST /sapi/v1/asset/transfer` with `MAIN_UMFUTURE` / `UMFUTURE_MAIN`  | `POST /api/v3/accounts/inner-transfer` between `main` and `contract` accts   |
| Sub→master/master→sub transfer | `POST /sapi/v1/sub-account/universalTransfer` (master)                 | `POST /api/v2/accounts/sub-transfer` (master) / `GET /api/v3/sub-accounts/transfer` |
| Sub-side transfer history      | `GET /sapi/v1/sub-account/sub/transfer/history`                        | `GET /api/v3/accounts/sub-transfer` filtered by sub                          |
| Deposit/withdraw history       | `GET /sapi/v1/capital/deposit/hisrec` + `…/withdraw/history`           | `GET /api/v1/deposits` + `GET /api/v1/withdrawals`                           |
| Flexible savings               | Simple Earn `/sapi/v1/simple-earn/flexible/{list,subscribe,redeem,position}` | Earn / Pool-X — `/api/v1/earn/saving/products` + `…/saving/purchases` etc.    |
| Margin mode + leverage         | `setMarginMode`, `setLeverage` (per symbol on USDM)                    | `setLeverage` (per contract; cross/isolated set by contract type)            |

ccxt's unified API gives us most of what we need (`fetchBalance`,
`fetchOrderBook`, `fetchFundingRates`, `createOrder`). The SAPI/private
endpoints for Earn, transfer history, and sub-account flows need direct
calls.

### Funding rate gotchas on KuCoin

- KuCoin perps use **8h funding** uniformly (no 4h windows like Binance has on
  some pairs). Our existing `_interval_hours` helper handles this; the field
  just always returns 8.
- Funding rate is paid on the *position notional × rate*, just like Binance.
  No semantic change.
- KuCoin lists fewer perps than Binance — maybe 100–200 vs Binance's ~400.
  Smaller universe.

### Earn product gotchas

KuCoin's Earn UI is messier than Binance's:

- **Pool-X / Earn Lending** is the closest analog to Binance Simple Earn
  Flexible. Subscribe with USDT and earn variable APR. Redemption is
  near-instant for "flexible" tier; "fixed" terms have lockups.
- The API is **less mature** than Binance's. Some endpoints are documented
  but undocumented in ccxt — we'd have to call them via the raw private
  request layer.
- KuCoin also has **structured products / dual investment** — those are
  *not* what we want. Stick to flexible savings only.

## 3. Architectural changes needed

### 3.1 Gateway abstraction

Today `BinanceGateway` is concrete and the bot imports it directly. To support
multiple exchanges cleanly:

```
app/exchanges/
  __init__.py          # registry / factory
  base.py              # abstract Gateway (Protocol or ABC)
  binance.py           # BinanceGateway (move from app/exchange.py)
  kucoin.py            # KucoinGateway (new)
```

`base.py` defines the contract every exchange must implement:

```python
class Gateway(Protocol):
    name: str  # 'binance' | 'kucoin'

    def load_markets(self) -> None: ...
    def safe_balances(self) -> dict | None: ...
    def safe_price(self, symbol: str, perp: bool = False) -> float | None: ...
    def scan_funding(self, ...) -> tuple[list[Candidate], int, list]: ...
    def order_book_depth_usdt(self, ...) -> float: ...

    # Orders
    def create_spot_buy(self, symbol, qty, paper, slippage_bps, fee_bps): ...
    def create_perp_short(self, ...): ...
    def close_spot(self, ...): ...
    def close_perp(self, ...): ...
    def configure_perp_for_arb(self, symbol) -> tuple[bool, str]: ...

    # Transfers
    def transfer_spot_to_futures(self, amount, paper) -> tuple[bool, str]: ...
    def transfer_futures_to_spot(self, amount, paper) -> tuple[bool, str]: ...

    # Earn
    def earn_subscribe(self, amount_usdt, paper) -> tuple[bool, str]: ...
    def earn_redeem(self, amount_usdt, paper) -> tuple[bool, str]: ...
    def earn_balance_usdt(self) -> tuple[float | None, str]: ...
    def earn_subscribe_asset(self, asset, amount, paper) -> tuple[bool, str]: ...
    def earn_redeem_asset(self, asset, amount, paper) -> tuple[bool, str]: ...
    def flexible_earn_apr(self, asset) -> float: ...

    # Capital flows
    def deposit_history(self, asset='USDT', lookback_days=365) -> list[dict]: ...
    def withdrawal_history(self, ...) -> list[dict]: ...
    def sub_account_transfer_history(self, ...) -> list[dict]: ...
    def net_injected_capital_usdt(self, lookback_days=365) -> tuple[float | None, dict]: ...

    # Misc
    def open_perp_positions_raw(self) -> list[dict]: ...
    last_balance_error: str
```

### 3.2 Exchange dimension on data

Add an `exchange` column to:
- `Position`
- `Trade`
- `EquityCurve`
- `BalanceSnapshot`
- `RejectedCandidate`
- `BotEvent`
- `CapitalFlow`
- `ScanResult`

Default to `'binance'` for back-compat. Migration adds the column and backfills.

`ModeState` / `StrategyConfig` / `EarnState` get keyed by `(mode, exchange)`
instead of just `(mode,)`. So you'd have e.g. `paper-binance`,
`paper-kucoin`, `live-binance`, `live-kucoin`.

### 3.3 Cycle changes

`run_one_cycle` iterates `(mode, exchange)` instead of just `mode`. Each pass
runs against its own gateway with its own state row. The candidate picker
(combined-yield rank) operates **per-exchange** because the trade is
same-exchange (spot leg + perp leg on the same venue). Cross-exchange arb is
explicitly out of scope for v1 because hedge-break risk during transfer
windows is too high.

### 3.4 Configuration

Most strategy params should be the same across exchanges. To keep the UI
simple, `StrategyConfig` stays a single row — it's exchange-agnostic. Per-
exchange tunables (which we don't really need yet — perp leverage, paper
slippage, etc. work the same) can be added later if they diverge.

### 3.5 Settings

Add KuCoin creds to `Settings`:

```python
kucoin_api_key: str = Field(default='')
kucoin_api_secret: str = Field(default='')
kucoin_api_passphrase: str = Field(default='')   # the third secret
```

Document in README that all three must be set for KuCoin to be active. If
any is missing, the gateway constructor returns a stub that reports
"unconfigured" on every call (no exceptions, just empty data).

### 3.6 UI

- Sidebar gets a per-exchange toggle alongside the per-mode tabs. So the
  user picks one of {paper-binance, live-binance, paper-kucoin, live-kucoin}.
- All filters on routes update to `WHERE mode = :v AND exchange = :ex`.
- Equity composition pie shows total across exchanges for the active mode,
  with each exchange as a segment.
- Net injected capital sums across exchanges.

## 4. Implementation phases (recommended order)

### Phase 0 — Gateway abstraction without behavior change (1–2 days)

- Move `app/exchange.py` → `app/exchanges/binance.py`.
- Create `app/exchanges/base.py` with the `Gateway` protocol/ABC.
- Update imports across the codebase. Bot still uses Binance-only via the
  factory. No data-model changes yet. Verify nothing breaks.

### Phase 1 — Add `exchange` column + factory selection (1 day)

- Schema migration adds `exchange` to all per-row tables, default `'binance'`.
- `ModeState`, `EarnState` re-keyed by `(mode, exchange)`.
- Cycle iterates a list of active exchanges, currently `['binance']`.

### Phase 2 — KucoinGateway implementation (2–3 days)

- Subclass / implement `Gateway` for KuCoin via ccxt + raw SAPI for the
  parts ccxt doesn't expose (Earn, sub-account transfers).
- Test against KuCoin sandbox first if available, then live with $5 to
  validate every path.

### Phase 3 — UI per-exchange tab (1 day)

- Sidebar gets exchange selector.
- All routes scope queries by exchange.
- Monitoring tab (already planned) shows both exchanges' raw state side
  by side.

### Phase 4 — Production hardening (ongoing)

- Cross-exchange diversity rule: skip a candidate if the same base asset
  is open on **any** exchange.
- Per-exchange rate limits (KuCoin's are tighter than Binance's; the
  current rate-limited ccxt clients should mostly handle this).
- Per-exchange Earn caveats — KuCoin's flexible-savings redemption can
  occasionally take a few seconds rather than being instant.

**Total effort: ~5–7 days of focused dev** before live trading on KuCoin.
Most of that is testing and edge-case handling, not the API integration
itself.

## 5. Risks specific to KuCoin

- **Earn API less polished** — flexible-savings products may not always
  be redeemable instantly. Pre-flight close logic should treat KuCoin
  Earn redeem as best-effort and fall back to manual top-up if it doesn't
  return promptly.
- **Smaller liquidity** — depth filter threshold may need to be lower
  per-exchange. Start with `min_order_book_depth_usdt` halved on KuCoin.
- **Funding paid in BTC for some pairs** — most are USDT-margined but a
  few use the underlying asset. Stick to USDT-margined linear perps
  (`*USDTM` symbols) for parity with our existing logic.
- **Settlement quirks** — KuCoin perps use a slightly different
  mark-price calculation. Basis filter (±20 bps default) may need
  retuning per-exchange.
- **Sub-account transfer latency** — master→sub on KuCoin can take a
  few seconds longer than Binance. Don't auto-transfer mid-cycle; only
  at well-defined points.

## 6. Open questions for you

1. Do you want KuCoin live and Binance live **at the same time**, or
   one at a time with a switch? (The architecture supports both; same-
   time is more capital-efficient but doubles the risk surface.)
2. Should diversity be **per-exchange** (can hold SOL on Binance and
   SOL on KuCoin simultaneously) or **global** (only one SOL position
   total)?
3. Any KuCoin-specific perps you definitely want included or excluded?
   E.g., `xMSTRUSDTM` if KuCoin lists it (they may not).

## 7. Recommendation

Do this in stages, not all at once. Phase 0 + Phase 1 alone are worth
shipping early — they make the codebase ready for KuCoin without
introducing live KuCoin trading. That gives you a clean checkpoint to
verify nothing broke before we add the KuCoin gateway and start trading
on it.

Once you confirm the answers in §6, I'll write the actual code.
