# Multi-venue integration plan (KuCoin first, IBKR later)

## Framing — one pool, many venues

The bot manages **a single pool of capital distributed across venues**. It is
not "the Binance dashboard plus a separate KuCoin dashboard"; it is one
strategy operating across whichever exchanges and brokers are configured. Today
the only live venue is Binance (spot + USDM-perp). KuCoin is next, and
Interactive Brokers (for cross-asset / equities-perp arb) sits behind that.

Concrete consequences of this framing:

* Every `Position` and `Trade` row carries an `exchange` tag. The same DB,
  the same UI, the same accounting code path serves all venues. Adding a
  venue is appending a row to the venues registry — not a new dashboard.
* The dashboard's equity composition donut and KPIs aggregate across venues
  by default. Per-venue subtotals are a breakdown of the pool, not an
  independent dashboard.
* The Monitoring tab is the operational view of the pool: pool total →
  per-venue capital subtotals → per-venue API probes. Same shape for every
  venue.
* The cross-venue rebalancer (future) is the natural extension of the
  intra-Binance Spot ↔ Futures ↔ Earn rebalancer the bot already runs every
  cycle. Same logic, wider scope.

## Phase 1 — KuCoin gateway

**Account setup**

* Create a sub-account for the bot (not the master). Restrict the API key to
  spot + futures trading only; no withdraw permission.
* Whitelist the bot's outbound IP (Coolify static egress IP, surfaced on the
  Safety tab).
* Enable both `kucoin` (spot) and `kucoinfutures` (USDT-margined perps) ccxt
  clients.

**Endpoint mapping (KuCoin → Binance equivalent)**

| Concept                   | Binance                                | KuCoin                                              |
| ------------------------- | -------------------------------------- | --------------------------------------------------- |
| Spot account              | `ccxt.binance().fetch_balance()`       | `ccxt.kucoin().fetch_balance()`                     |
| USDT-perp account         | `ccxt.binanceusdm().fetch_balance()`   | `ccxt.kucoinfutures().fetch_balance()`              |
| Funding rates             | `binanceusdm.fetch_funding_rates()`    | `kucoinfutures.fetch_funding_rates()` (per symbol)  |
| Spot ↔ futures transfer   | `sapiPostAssetTransfer` (universal)    | `privateInnerTransferV2` SAPI on the spot client    |
| Earn / Lend               | Simple Earn Flexible                   | Pool-X (auto-lend); separate gateway optional       |
| Order-book depth          | `fetch_order_book(symbol, limit=50)`   | Same — ccxt-uniform                                 |
| Cross margin / leverage   | `set_margin_mode('cross')` + `set_leverage(1)` | Same shape; both clients accept it           |
| Sub-account transfer      | `sapiV1GetSubAccountSubTransferHistory` | Universal-transfer endpoint w/ sub-account ID      |

**Gateway abstraction** — what `BinanceGateway` already does, generalized

```python
class VenueGateway(Protocol):
    venue_id: str  # 'binance' | 'kucoin' | 'ibkr'

    def safe_balances(self) -> dict | None: ...
    def scan_funding(self, ...) -> tuple[list[Candidate], int, list[tuple]]: ...
    def order_book_depth_usdt(self, symbol, side, band_bps, perp) -> float: ...
    def market_min_amount(self, symbol, perp) -> float: ...
    def configure_perp_for_arb(self, symbol) -> tuple[bool, str]: ...
    def create_spot_buy(...): ...
    def create_perp_short(...): ...
    def close_spot(...): ...
    def close_perp(...): ...
    def safe_price(self, symbol, perp) -> float | None: ...
    def transfer_spot_to_futures(...): ...
    def transfer_futures_to_spot(...): ...
    def net_injected_capital_usdt(...) -> tuple[float | None, dict]: ...
    # Earn is opt-in per gateway; venues without an Earn equivalent return 0.
    def earn_balance_usdt(self) -> tuple[float | None, str]: ...
    def earn_subscribe(self, amount_usdt, paper_mode) -> tuple[bool, str]: ...
    def earn_redeem(self, amount_usdt, paper_mode) -> tuple[bool, str]: ...
```

`BinanceGateway` already implements this surface; `KuCoinGateway` will mirror
it. The bot currently constructs `BinanceGateway()` directly in
`app.bot.run_loop` and `app.main`. After Phase 1:

```python
def make_gateways(cfg) -> list[VenueGateway]:
    gws = [BinanceGateway()] if cfg.binance_enabled else []
    if cfg.kucoin_enabled:
        gws.append(KuCoinGateway())
    return gws
```

The cycle then iterates over `gws` for each scan / open / close, comparing
candidates across venues and routing each entry to whichever venue has the
better combined yield (funding APY + spot earn) net of estimated taker fees
and transfer cost.

## Phase 2 — Cross-venue rebalancing

Once two venues are live:

1. Each cycle, compute the pool's free USDT distribution across venues.
2. If venue A has a candidate scoring `combined_apy_A` and free USDT < min
   notional, but venue B has free USDT > min notional **and** its best
   candidate scores `combined_apy_B < combined_apy_A − rebalance_cost_bps`,
   move USDT B → A.
3. Transfer cost = withdraw fee + taker fee + slippage on both sides. The
   bot won't rebalance unless the yield delta covers the round-trip cost
   over the expected hold period.
4. Same rebalance threshold pattern as the intra-Binance one already running
   today (`auto_rebalance_threshold` in `StrategyConfig`).

Liquidity / network constraints to respect:

* USDT on different chains (TRC20 / ERC20 / BEP20) — each venue has a
  preferred chain. The bot needs a "chain matrix" lookup so it always
  withdraws on the cheapest mutual chain.
* Withdraw whitelists (mandatory on KuCoin sub-accounts) — pre-register
  every counterparty venue's deposit address before enabling rebalancing.
* Withdrawal status (`pending` / `processing`) — treat in-flight USDT as
  earmarked, neither in the source venue's free balance nor the
  destination's, until confirmed.

## Phase 3 — Interactive Brokers (cross-asset)

This is the interesting one and motivates the single-pool framing more than
KuCoin does. Some of the highest funding rates the bot already sees are on
**single-stock perps** like `INCL-USDT-PERP`, `MSTR-USDT-PERP`. Today we
can't take those: the spot leg of the arb requires holding the actual stock
on a brokerage that supports fractional shares and US equities — i.e. IBKR.

Sketch:

* `IBKRGateway` implements the same `VenueGateway` protocol. Its "spot"
  client is the IBKR REST / TWS API; its "perp" client is whichever crypto
  venue lists the corresponding stock-perp (Binance lists some).
* The arb is split-venue: long stock at IBKR, short stock-perp at Binance.
  Both legs settle in USD / USDT; the bot tracks the cross-venue hedge in
  the same `Position` row, just with different `exchange` tags on the
  `Trade` rows that built each leg.
* This is genuinely new territory — separate FX risk (USD vs USDT), a
  separate margin pool, and tax treatment differs from pure-crypto arb.
* Defer to its own design pass once Phase 2 is solid.

## Backwards-compatibility commitments

* `Position.exchange` and `Trade.exchange` default to `'binance'` so every
  existing row reads correctly on first migrated load.
* The dashboard / transactions / monitoring pages all render the venue tag
  from `exchange`, with the historical Binance value baked in.
* The bot continues to call `BinanceGateway` directly for now; Phase 1
  adds the gateway-list seam without changing bot behavior.
* Paper mode stays a single virtual venue tagged `paper`; no per-venue
  paper accounting until users actually want it.

## Open questions

1. **KuCoin sub-account vs master?** Sub-account is safer (a compromised
   key only drains that sub-account) but the universal-transfer endpoint
   shapes differ between master and sub. Recommendation: sub-account; live
   with the slightly more annoying transfer code path.
2. **Earn equivalent on KuCoin?** Pool-X auto-lend exists but pays in the
   asset, not USDT, with variable rates that make the "compounded APY"
   accounting messier. Defer Earn integration on KuCoin past Phase 1.
3. **Funding-rate frequency parity?** KuCoin pays funding every 8 hours
   (same as Binance); this is the easy case. IBKR's stock-perp funding is
   daily. The annualization helper in `app.exchange.annualize_rate`
   already takes interval hours as an argument so this is purely a
   gateway-config concern.
