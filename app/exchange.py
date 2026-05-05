"""Venue gateways — the bot's single point of contact for every exchange / broker.

The portfolio is a single pool of capital distributed across venues. Each
venue (Binance live today; KuCoin live from Phase 1; Interactive Brokers in
Phase 3) presents the same surface to the bot through :class:`VenueGateway`,
so the cycle loop in :mod:`app.bot` iterates over a list of gateways without
any per-venue conditionals.

Class hierarchy
---------------
* :class:`VenueGateway` — abstract base. Holds every method whose
  implementation is identical across venues thanks to ccxt's uniform
  facade (order-book depth, balances, prices, scanning, market orders,
  hedge configuration). Subclasses must populate ``self.spot`` and
  ``self.futures`` ccxt clients and override venue-specific methods.
* :class:`BinanceGateway` — Binance spot + USDM-perp + Simple Earn +
  universal-transfer SAPI + deposit/withdrawal/sub-transfer history.
* :class:`KuCoinGateway` — KuCoin spot + futures + ``innerTransfer``
  SAPI. Earn (Pool-X) and capital-flow history are deferred — earn
  methods return safe no-ops; ``net_injected_capital_usdt`` returns
  ``None`` so the dashboard falls back to manual capital flows.

Design invariants
-----------------
* Every method that touches the network is wrapped to fail-soft: it
  returns ``None`` / ``[]`` / ``(False, err)`` rather than letting an
  exception escape into the cycle loop. A transient outage on one
  venue must not break the cycle for the others.
* SAPI methods are looked up through a small list of ccxt naming
  conventions (``sapiV1Get…`` / ``sapi_v1_get_…``) so the gateway
  works across the ccxt-version drift between dev and production.
* Caches (``_earn_product_id_cache``, ``_*_history_cache``,
  ``_earn_subscribe_cooldown_until``) live on the instance with
  explicit TTLs. They get cleared by recreating the gateway, e.g. on
  process restart — that's intentional.
* Binance error codes the bot reacts to are named constants; the
  string-match-on-exception-text approach is what ccxt's stable
  surface gives us.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import ccxt

from app.config import settings


# ─── Binance error codes we react to ────────────────────────────────────────
# Returned in the ccxt exception message text when Binance rejects a request.
# We string-search rather than parse JSON because ccxt's message format
# isn't fully stable across versions. KuCoin error codes are not surfaced
# explicitly today — we treat any ccxt exception as "the call failed,
# fall back gracefully" and log the message verbatim.

BINANCE_ERR_INVALID_API = '-2015'           # bad key / IP not whitelisted / missing permission
BINANCE_ERR_INSUFFICIENT_MARGIN = '-2019'   # futures order can't be placed for lack of free margin
BINANCE_ERR_MALFORMED_AMOUNT = '-1102'      # amount param missing/empty/zero on a SAPI call
BINANCE_ERR_NO_EARN_POSITION = '-6053'      # trying to redeem from a flexible product with no balance
BINANCE_ERR_EARN_TOO_MANY_SUBS = '77505'    # subscribed too many times for this token (rate-limit)


# ─── Candidate dataclass ────────────────────────────────────────────────────
# A scan result row: one funding-rate opportunity that passed every filter
# (entry threshold, volume, liquidity, etc.). The bot ranks candidates by
# ``combined_apy`` to decide which to open next.

@dataclass
class Candidate:
    spot_symbol: str
    perp_symbol: str
    funding_rate: float
    funding_interval_hours: float
    quote_volume: float
    spot_depth_usdt: float = 0.0
    perp_depth_usdt: float = 0.0
    spot_earn_apr: float = 0.0  # latest annualized rate on the asset's flexible earn product, if any
    venue_id: str = 'binance'   # which venue this candidate came from — set by scan_funding

    @property
    def funding_apr(self) -> float:
        return annualize_rate(self.funding_rate, self.funding_interval_hours)

    @property
    def funding_apy(self) -> float:
        # Same compounded value as funding_apr — kept for callers that prefer the explicit name.
        return self.funding_apr

    @property
    def combined_apy(self) -> float:
        """Funding APY (compounded) + spot Earn APR (Binance's reported flexible rate).
        This is the headline yield used for ranking — it captures both legs' income."""
        return self.funding_apr + self.spot_earn_apr

    @property
    def min_depth_usdt(self) -> float:
        return min(self.spot_depth_usdt, self.perp_depth_usdt)


def annualize_rate(period_rate: float, interval_hours: float) -> float:
    """Compounded annualization (APY) — what you'd actually make over a year if the
    per-period rate persisted and every payout were reinvested.
    APY = (1 + period_rate) ** periods_per_year − 1.
    For an 8h interval, periods_per_year ≈ 1095."""
    if interval_hours <= 0:
        return 0.0
    if period_rate <= -1.0:
        return -1.0  # would imply >100% loss per period; clamp instead of complex math
    periods = 24.0 * 365.0 / interval_hours
    try:
        return (1.0 + period_rate) ** periods - 1.0
    except OverflowError:
        return float('inf') if period_rate > 0 else -1.0


def _interval_hours(row: dict) -> float:
    """Extract funding interval (in hours) from a ccxt ``fetch_funding_rates`` row.
    Falls back to 8h — Binance's default — when the response doesn't carry the
    interval explicitly. KuCoin pays funding every 8h so the same default works."""
    interval = row.get('interval')
    if isinstance(interval, str) and interval.endswith('h'):
        try:
            return float(interval[:-1])
        except ValueError:
            pass
    fund_ts = row.get('fundingTimestamp')
    next_ts = row.get('nextFundingTimestamp')
    if fund_ts and next_ts and next_ts > fund_ts:
        hours = (next_ts - fund_ts) / 3_600_000.0
        if 1.0 <= hours <= 24.0:
            return round(hours)
    return 8.0


# ─── VenueGateway base class ────────────────────────────────────────────────
# Shared implementation for every method that's identical across ccxt-supported
# venues. Subclasses must:
#   * set the class attributes ``venue_id`` and ``name``
#   * populate ``self.spot`` and ``self.futures`` in ``__init__``
#   * implement the venue-specific methods at the bottom (transfers, earn,
#     history). Default implementations are safe no-ops so a venue without
#     Earn (e.g. KuCoin Phase 1) still has a complete surface.

class VenueGateway:
    venue_id: str = ''   # 'binance' | 'kucoin' | 'ibkr'
    name: str = ''       # human-readable label rendered in UI

    def __init__(self) -> None:
        # Subclasses must set these in their own __init__ before super().
        self.spot = None
        self.futures = None
        self.last_balance_error: str = ''

    # ─── Market data + read-only helpers (ccxt-uniform) ───────────────────

    def load_markets(self) -> None:
        self.spot.load_markets()
        self.futures.load_markets()

    def order_book_depth_usdt(self, symbol: str, side: str = 'ask', band_bps: float = 10.0, perp: bool = False) -> float:
        """Sum of USDT-equivalent quantity on `side` of the book within `band_bps`
        of mid. side='ask' for buying (we hit asks) — used for the spot leg open
        and the perp short close. side='bid' for selling — used for the spot
        close and the perp short open. Returns 0.0 on any error."""
        try:
            ex = self.futures if perp else self.spot
            ob = ex.fetch_order_book(symbol, limit=50)
        except Exception:
            return 0.0
        bids = ob.get('bids') or []
        asks = ob.get('asks') or []
        if not bids or not asks:
            return 0.0
        try:
            mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
        except Exception:
            return 0.0
        if mid <= 0:
            return 0.0
        threshold = band_bps / 10000.0
        if side == 'ask':
            cutoff = mid * (1 + threshold)
            levels = asks
            within = lambda p: p <= cutoff  # noqa: E731
        else:
            cutoff = mid * (1 - threshold)
            levels = bids
            within = lambda p: p >= cutoff  # noqa: E731
        total = 0.0
        for lvl in levels:
            try:
                p, q = float(lvl[0]), float(lvl[1])
            except Exception:
                continue
            if not within(p):
                break
            total += p * q
        return total

    def safe_balances(self) -> dict | None:
        """Return ``{'spot': {...}, 'futures': {...}}`` from ccxt, or ``None`` on
        error (with the failure message pinned to ``self.last_balance_error``)."""
        try:
            result = {'spot': self.spot.fetch_balance(), 'futures': self.futures.fetch_balance()}
            self.last_balance_error = ''
            return result
        except Exception as e:
            self.last_balance_error = str(e)
            return None

    def price(self, symbol: str) -> float:
        return float(self.spot.fetch_ticker(symbol)['last'])

    def perp_price(self, symbol: str) -> float:
        return float(self.futures.fetch_ticker(symbol)['last'])

    def safe_price(self, symbol: str, perp: bool = False) -> float | None:
        try:
            return self.perp_price(symbol) if perp else self.price(symbol)
        except Exception:
            return None

    def market_min_amount(self, symbol: str, perp: bool = False) -> float:
        """Minimum order quantity (LOT_SIZE.minQty on Binance, baseMinSize on
        KuCoin) for a symbol. Both are surfaced as ``market.limits.amount.min``
        in ccxt. The bot uses this to detect dust on a closing leg and treat it
        as already-flat rather than spinning on rejected orders. Returns 0.0
        when the market isn't loaded — caller proceeds with the order."""
        ex = self.futures if perp else self.spot
        try:
            market = ex.market(symbol)
        except Exception:
            return 0.0
        try:
            return float((market.get('limits') or {}).get('amount', {}).get('min') or 0.0)
        except Exception:
            return 0.0

    def open_perp_positions_raw(self) -> list[dict]:
        """Open perp positions on this venue. Filters out zero-contract rows that
        ccxt sometimes returns for symbols that were once held."""
        try:
            positions = self.futures.fetch_positions()
        except Exception:
            return []
        return [p for p in positions if abs(float(p.get('contracts') or p.get('info', {}).get('positionAmt') or 0)) > 0]

    # ─── Funding scan (ccxt-uniform) ───────────────────────────────────────

    def scan_funding(
        self,
        entry_apr_threshold: float,
        min_quote_volume: float,
        min_depth_usdt: float = 0.0,
        depth_band_bps: float = 10.0,
        include_earn_apr: bool = False,
    ) -> tuple[list[Candidate], int, list[tuple[str, str, float]]]:
        """Scan this venue's USDT-perp funding rates. Returns
        ``(passing, total_examined, rejected)`` where:
        * ``passing`` is the ranked list of :class:`Candidate` rows that survived
          every filter (APR threshold, quote volume, liquidity).
        * ``total_examined`` is the number of perps whose funding rate ccxt
          returned — useful to confirm the API is actually live.
        * ``rejected`` is a list of ``(symbol, reason, apr)`` for the Logs tab.

        Each :class:`Candidate` carries ``venue_id`` so when multiple venues'
        results get pooled in the bot loop, the destination is unambiguous."""
        try:
            rates = self.futures.fetch_funding_rates()
        except Exception:
            return [], 0, []
        passing: list[Candidate] = []
        rejected: list[tuple[str, str, float]] = []
        total = 0
        for symbol, row in rates.items():
            fr = row.get('fundingRate')
            if fr is None or not symbol.endswith(':USDT'):
                continue
            total += 1
            interval_h = _interval_hours(row)
            apr = annualize_rate(float(fr), interval_h)
            if apr < entry_apr_threshold:
                continue
            base = symbol.split('/')[0]
            spot_symbol = f'{base}/USDT'
            if spot_symbol not in self.spot.markets:
                rejected.append((symbol, 'no_spot_market', apr))
                continue
            try:
                t = self.spot.fetch_ticker(spot_symbol)
            except Exception:
                rejected.append((symbol, 'ticker_error', apr))
                continue
            qv = float(t.get('quoteVolume') or 0)
            if qv < min_quote_volume:
                rejected.append((symbol, f'volume<{min_quote_volume:.0f}', apr))
                continue
            spot_depth = perp_depth = 0.0
            if min_depth_usdt > 0:
                spot_depth = self.order_book_depth_usdt(spot_symbol, side='ask', band_bps=depth_band_bps, perp=False)
                perp_depth = self.order_book_depth_usdt(symbol, side='bid', band_bps=depth_band_bps, perp=True)
                tight = min(spot_depth, perp_depth)
                if tight < min_depth_usdt:
                    rejected.append((symbol, f'depth<{min_depth_usdt:.0f} (spot {spot_depth:.0f} / perp {perp_depth:.0f} @ ±{depth_band_bps:.0f}bps)', apr))
                    continue
            earn_apr = self.flexible_earn_apr(base) if include_earn_apr else 0.0
            passing.append(Candidate(
                spot_symbol=spot_symbol,
                perp_symbol=symbol,
                funding_rate=float(fr),
                funding_interval_hours=interval_h,
                quote_volume=qv,
                spot_depth_usdt=spot_depth,
                perp_depth_usdt=perp_depth,
                spot_earn_apr=earn_apr,
                venue_id=self.venue_id,
            ))
        # Rank by total expected yield (funding APY + spot earn APR). Ties
        # break by deeper book — deeper markets tend to be high-volume names
        # with lower friction.
        passing.sort(key=lambda c: (c.combined_apy, c.min_depth_usdt), reverse=True)
        return passing, total, rejected

    # ─── Order placement (ccxt-uniform) ───────────────────────────────────

    def _market_order(self, leg: str, symbol: str, side: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float) -> dict:
        """Place a market order on the spot or futures client. In ``paper_mode``
        synthesise a fill at the current ticker price, shifted adversely by
        ``slippage_bps`` and charged ``fee_bps`` — these numbers are
        configurable on the Configuration tab so paper trading tracks reality.

        ``leg`` is 'spot' | 'futures'; this is the trade-row leg label that
        ends up in :data:`app.models.Trade.venue` (which long predates the
        cross-venue ``exchange`` column)."""
        if paper_mode:
            mid = self.perp_price(symbol) if leg == 'futures' else self.price(symbol)
            slip = slippage_bps / 10000.0
            fill_price = mid * (1 + slip) if side == 'buy' else mid * (1 - slip)
            fee_cost = fill_price * amount * (fee_bps / 10000.0)
            return {
                'id': 'paper', 'symbol': symbol, 'side': side, 'amount': amount,
                'venue': leg, 'status': 'closed', 'price': fill_price, 'fee': {'cost': fee_cost},
            }
        if leg == 'spot':
            return self.spot.create_order(symbol, 'market', side, amount)
        return self.futures.create_order(symbol, 'market', side, amount)

    def create_spot_buy(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float) -> dict:
        return self._market_order('spot', symbol, 'buy', amount, paper_mode, slippage_bps, fee_bps)

    def create_perp_short(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float) -> dict:
        return self._market_order('futures', symbol, 'sell', amount, paper_mode, slippage_bps, fee_bps)

    def close_spot(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float) -> dict:
        return self._market_order('spot', symbol, 'sell', amount, paper_mode, slippage_bps, fee_bps)

    def close_perp(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float) -> dict:
        return self._market_order('futures', symbol, 'buy', amount, paper_mode, slippage_bps, fee_bps)

    # ─── Margin mode + leverage (ccxt-uniform — both Binance & KuCoin) ────

    def configure_perp_for_arb(self, symbol: str) -> tuple[bool, str]:
        """Set the perp to CROSS margin at 1x leverage — the right setup for a
        delta-neutral arb hedge:
          * CROSS shares margin across positions, avoiding single-leg
            liquidation risk on tiny price moves.
          * 1x leverage means perp margin == perp notional == spot notional,
            keeping both legs symmetrically capitalised.
        Both calls are idempotent on Binance and KuCoin — repeating them when
        already-set is cheap and returns a benign "no need to change"."""
        margin_ok = leverage_ok = True
        margin_err = leverage_err = ''
        try:
            self.futures.set_margin_mode('cross', symbol)
        except Exception as e:
            msg = str(e)
            if 'no need to change' not in msg.lower() and 'already' not in msg.lower():
                margin_ok, margin_err = False, msg
        try:
            self.futures.set_leverage(1, symbol)
        except Exception as e:
            msg = str(e)
            if 'no need to change' not in msg.lower() and 'already' not in msg.lower():
                leverage_ok, leverage_err = False, msg
        if margin_ok and leverage_ok:
            return True, ''
        return False, '; '.join(filter(None, [margin_err, leverage_err]))

    # ─── Default no-op implementations for venue-specific surfaces ────────
    # Subclasses override the methods their venue actually supports.

    def flexible_earn_apr(self, asset: str, ttl_seconds: float = 3600.0) -> float:
        """Latest annualized rate (decimal) for the venue's flexible Earn product
        on `asset`. Default: 0.0 (venue has no Earn or it's not yet wired)."""
        return 0.0

    def earn_balance_usdt(self) -> tuple[float | None, str]:
        return 0.0, ''

    def earn_balance(self, asset: str = 'USDT') -> tuple[float | None, str]:
        return 0.0, ''

    def earn_subscribe(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        return self.earn_subscribe_asset('USDT', amount_usdt, paper_mode)

    def earn_subscribe_asset(self, asset: str, amount: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, f'{asset}: Earn not wired on {self.name}'

    def earn_redeem(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        return self.earn_redeem_asset('USDT', amount_usdt, paper_mode)

    def earn_redeem_asset(self, asset: str, amount: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, f'{asset}: Earn not wired on {self.name}'

    def transfer_spot_to_futures(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, f'spot→futures transfer not wired on {self.name}'

    def transfer_futures_to_spot(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, f'futures→spot transfer not wired on {self.name}'

    def net_injected_capital_usdt(self, lookback_days: int = 365) -> tuple[float | None, dict]:
        """Returns ``(net, meta)`` where ``net`` is in USDT and ``meta`` carries
        a per-component breakdown for the UI. ``None`` signals the caller to
        fall back to manual CapitalFlow rows."""
        return None, {'error': f'history not wired on {self.name}'}


# ─── Binance gateway ────────────────────────────────────────────────────────
# Implements the full surface — Earn, universal-transfer, deposit /
# withdrawal / sub-account-transfer history. Uses Binance's USDM-futures
# client (``ccxt.binanceusdm``) to keep symbol shape identical to the spot
# client (``BTC/USDT:USDT`` for the perp, ``BTC/USDT`` for the spot).

class BinanceGateway(VenueGateway):
    venue_id = 'binance'
    name = 'Binance'

    # Earn-subscribe rate-limit machinery — kept as class state so all
    # instances share the cooldown (the bot creates fresh gateways
    # frequently for HTTP routes).
    _earn_subscribe_cooldown_until: dict[str, float] = {}
    EARN_SUBSCRIBE_DEFAULT_COOLDOWN_S = 3600.0       # 1h between subscribes per asset
    EARN_SUBSCRIBE_RATE_LIMITED_COOLDOWN_S = 86400.0  # 24h after Binance signals 77505

    # Caches — instance-scoped so they survive across method calls but die
    # with the gateway. Each carries an explicit TTL because Earn-product
    # ids and capital-flow history are stable for many minutes.
    _flexible_earn_apr_cache: dict[str, tuple[float, float]]
    _earn_product_id_cache: dict[str, str]
    _deposit_history_cache: dict[str, tuple[list[dict], float]]
    _withdrawal_history_cache: dict[str, tuple[list[dict], float]]
    _sub_transfer_history_cache: dict[str, tuple[list[dict], float]]

    def __init__(self) -> None:
        super().__init__()
        self.spot = ccxt.binance({
            'apiKey': settings.binance_api_key,
            'secret': settings.binance_api_secret,
            'enableRateLimit': True,
        })
        self.futures = ccxt.binanceusdm({
            'apiKey': settings.binance_api_key,
            'secret': settings.binance_api_secret,
            'enableRateLimit': True,
        })
        # Per-instance caches so cleared state doesn't leak across processes.
        self._flexible_earn_apr_cache = {}
        self._earn_product_id_cache = {}
        self._deposit_history_cache = {}
        self._withdrawal_history_cache = {}
        self._sub_transfer_history_cache = {}

    # ─── SAPI helper (handles ccxt-version naming drift) ───────────────────

    def _call_sapi(self, candidates: tuple[str, ...], params: dict | None = None):
        """Try each candidate ccxt method name in order; return the first hit.
        Returns ``None`` when none of the candidates are bound on the spot
        client (older ccxt builds expose different names than newer ones)."""
        for name in candidates:
            fn = getattr(self.spot, name, None)
            if callable(fn):
                return fn(params or {})
        return None

    # ─── Binance Simple Earn (Flexible) ────────────────────────────────────

    def flexible_earn_apr(self, asset: str, ttl_seconds: float = 3600.0) -> float:
        """Latest annualized rate (decimal) for the flexible Earn product on
        `asset`, cached for an hour. Returns 0.0 if no product is offered or
        the API isn't reachable — caller treats that as "no extra spot yield
        to count toward the candidate's combined APY"."""
        cached = self._flexible_earn_apr_cache.get(asset)
        if cached:
            rate, ts = cached
            if time.time() - ts < ttl_seconds:
                return rate
        try:
            resp = self._call_sapi((
                'sapiV1GetSimpleEarnFlexibleList',
                'sapi_v1_get_simple_earn_flexible_list',
                'sapiGetSimpleEarnFlexibleList',
            ), {'asset': asset})
        except Exception:
            self._flexible_earn_apr_cache[asset] = (0.0, time.time())
            return 0.0
        if not resp:
            self._flexible_earn_apr_cache[asset] = (0.0, time.time())
            return 0.0
        for r in resp.get('rows', []) or resp.get('data', []) or []:
            if r.get('asset') == asset:
                try:
                    rate = float(r.get('latestAnnualPercentageRate') or r.get('annualPercentageRate') or 0)
                except Exception:
                    rate = 0.0
                self._flexible_earn_apr_cache[asset] = (rate, time.time())
                return rate
        self._flexible_earn_apr_cache[asset] = (0.0, time.time())
        return 0.0

    def earn_product_id(self, asset: str) -> str | None:
        cached = self._earn_product_id_cache.get(asset)
        if cached:
            return cached
        try:
            resp = self._call_sapi((
                'sapiV1GetSimpleEarnFlexibleList',
                'sapi_v1_get_simple_earn_flexible_list',
                'sapiGetSimpleEarnFlexibleList',
            ), {'asset': asset})
        except Exception:
            return None
        if not resp:
            return None
        rows = resp.get('rows') or resp.get('data') or []
        for r in rows:
            if r.get('asset') == asset:
                pid = r.get('productId') or r.get('id')
                if pid:
                    self._earn_product_id_cache[asset] = str(pid)
                    return str(pid)
        return None

    def earn_balance(self, asset: str = 'USDT') -> tuple[float | None, str]:
        try:
            resp = self._call_sapi((
                'sapiV1GetSimpleEarnFlexiblePosition',
                'sapi_v1_get_simple_earn_flexible_position',
                'sapiGetSimpleEarnFlexiblePosition',
            ), {'asset': asset})
        except Exception as e:
            return None, str(e)
        if resp is None:
            return None, 'sapi method not available in ccxt'
        total = 0.0
        for r in resp.get('rows', []) or []:
            total += float(r.get('totalAmount') or 0)
        return total, ''

    def earn_balance_usdt(self) -> tuple[float | None, str]:
        return self.earn_balance('USDT')

    def earn_subscribe_asset(self, asset: str, amount: float, paper_mode: bool) -> tuple[bool, str]:
        """Subscribe ``amount`` of ``asset`` to Binance Simple Earn Flexible.
        Cooldown: each successful subscribe pins a per-asset cooldown so the
        bot doesn't spam the SAPI on every loop. 77505 from Binance bumps
        the cooldown to 24h for that asset to keep us safely under the daily
        rate-limit window."""
        if paper_mode:
            return True, 'paper'
        if amount <= 0:
            return False, f'{asset}: zero amount'
        cool_until = self._earn_subscribe_cooldown_until.get(asset, 0.0)
        now = time.time()
        if now < cool_until:
            remaining = int(cool_until - now)
            return False, f'{asset}: subscribe cooldown active ({remaining}s remaining; avoids Binance 77505 rate-limit)'
        pid = self.earn_product_id(asset)
        if not pid:
            return False, f'no flexible product for {asset}'
        try:
            resp = self._call_sapi((
                'sapiV1PostSimpleEarnFlexibleSubscribe',
                'sapi_v1_post_simple_earn_flexible_subscribe',
                'sapiPostSimpleEarnFlexibleSubscribe',
            ), {'productId': pid, 'amount': f'{amount:.6f}'})
        except Exception as e:
            err = str(e)
            if BINANCE_ERR_EARN_TOO_MANY_SUBS in err:
                self._earn_subscribe_cooldown_until[asset] = now + self.EARN_SUBSCRIBE_RATE_LIMITED_COOLDOWN_S
                return False, f'{asset}: Binance rate-limited (77505). Backing off 24h before retrying. {err[:120]}'
            return False, f'{asset}: {err}'
        if resp is None:
            return False, f'{asset}: sapi method not available in ccxt'
        # Success — install standard cooldown so we don't immediately retry next cycle.
        self._earn_subscribe_cooldown_until[asset] = now + self.EARN_SUBSCRIBE_DEFAULT_COOLDOWN_S
        return bool(resp.get('success', True)), ''

    def earn_redeem_asset(self, asset: str, amount: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount <= 0:
            return False, f'{asset}: zero amount'
        # Binance returns -1102 when amount string is "0.00" — skip clearly-tiny calls.
        if asset == 'USDT' and amount < 0.10:
            return False, f'amount {amount:.4f} below USDT redeem minimum'
        pid = self.earn_product_id(asset)
        if not pid:
            return False, f'no flexible product for {asset}'
        try:
            resp = self._call_sapi((
                'sapiV1PostSimpleEarnFlexibleRedeem',
                'sapi_v1_post_simple_earn_flexible_redeem',
                'sapiPostSimpleEarnFlexibleRedeem',
            ), {'productId': pid, 'amount': f'{amount:.6f}', 'destAccount': 'SPOT'})
        except Exception as e:
            return False, f'{asset}: {e}'
        if resp is None:
            return False, f'{asset}: sapi method not available in ccxt'
        return bool(resp.get('success', True)), ''

    # ─── Binance universal transfer (spot ⇄ USDM-futures) ─────────────────

    def _universal_transfer(self, transfer_type: str, amount_usdt: float) -> tuple[bool, str]:
        for name in ('sapiPostAssetTransfer', 'sapi_post_asset_transfer'):
            fn = getattr(self.spot, name, None)
            if callable(fn):
                try:
                    fn({'type': transfer_type, 'asset': 'USDT', 'amount': f'{amount_usdt:.2f}'})
                    return True, ''
                except Exception as e:
                    return False, str(e)
        return False, 'sapiPostAssetTransfer not available in this ccxt build'

    def transfer_spot_to_futures(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        return self._universal_transfer('MAIN_UMFUTURE', amount_usdt)

    def transfer_futures_to_spot(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        return self._universal_transfer('UMFUTURE_MAIN', amount_usdt)

    # ─── Deposit / withdrawal / sub-transfer history ──────────────────────
    # Binance returns up to 90 days per call on deposit/withdrawal endpoints
    # and 30 days per call on sub-account-transfer. We walk backwards in
    # chunks to reach ``lookback_days`` total. Cached for 5 minutes so the
    # dashboard's repeated renders don't burn rate-limit.

    def _walk_history(self, sapi_candidates: tuple[str, ...], asset: str, status_value: int, lookback_days: int) -> list[dict]:
        rows: list[dict] = []
        chunk_ms = 90 * 86400 * 1000
        end = int(time.time() * 1000)
        oldest = end - lookback_days * 86400 * 1000
        cursor = end
        while cursor > oldest:
            start = max(oldest, cursor - chunk_ms)
            try:
                resp = self._call_sapi(sapi_candidates, {'coin': asset, 'startTime': start, 'endTime': cursor})
            except Exception:
                break
            if not resp:
                break
            chunk = resp if isinstance(resp, list) else resp.get('rows') or resp.get('data') or []
            for r in chunk:
                if r.get('status') == status_value or status_value == -1:
                    rows.append(r)
            if cursor - chunk_ms <= oldest:
                break
            cursor = start
        return rows

    def deposit_history(self, asset: str = 'USDT', lookback_days: int = 365, ttl_seconds: float = 300.0) -> list[dict]:
        key = f'{asset}:{lookback_days}'
        cached = self._deposit_history_cache.get(key)
        if cached and (time.time() - cached[1]) < ttl_seconds:
            return cached[0]
        rows = self._walk_history((
            'sapiV1GetCapitalDepositHisrec',
            'sapi_v1_get_capital_deposit_hisrec',
            'sapiGetCapitalDepositHisrec',
        ), asset, status_value=1, lookback_days=lookback_days)  # 1 = success
        self._deposit_history_cache[key] = (rows, time.time())
        return rows

    def withdrawal_history(self, asset: str = 'USDT', lookback_days: int = 365, ttl_seconds: float = 300.0) -> list[dict]:
        key = f'{asset}:{lookback_days}'
        cached = self._withdrawal_history_cache.get(key)
        if cached and (time.time() - cached[1]) < ttl_seconds:
            return cached[0]
        rows = self._walk_history((
            'sapiV1GetCapitalWithdrawHistory',
            'sapi_v1_get_capital_withdraw_history',
            'sapiGetCapitalWithdrawHistory',
        ), asset, status_value=6, lookback_days=lookback_days)  # 6 = completed
        self._withdrawal_history_cache[key] = (rows, time.time())
        return rows

    def sub_account_transfer_history(self, asset: str = 'USDT', incoming: bool = True, lookback_days: int = 365, ttl_seconds: float = 300.0) -> list[dict]:
        """Sub-account-side view of master ↔ sub transfers.
        Endpoint: GET /sapi/v1/sub-account/sub/transfer/history
        type: 1 = transfer in (master → this sub), 2 = transfer out (sub → master).
        Returns [] if the API key isn't on a sub-account or the endpoint isn't
        accessible (master keys hit a different endpoint)."""
        key = f'{asset}:{1 if incoming else 2}:{lookback_days}'
        cached = self._sub_transfer_history_cache.get(key)
        if cached and (time.time() - cached[1]) < ttl_seconds:
            return cached[0]
        rows: list[dict] = []
        chunk_ms = 30 * 86400 * 1000
        end = int(time.time() * 1000)
        oldest = end - lookback_days * 86400 * 1000
        cursor = end
        while cursor > oldest:
            start = max(oldest, cursor - chunk_ms)
            try:
                resp = self._call_sapi((
                    'sapiV1GetSubAccountSubTransferHistory',
                    'sapi_v1_get_sub_account_sub_transfer_history',
                    'sapiGetSubAccountSubTransferHistory',
                ), {'asset': asset, 'type': 1 if incoming else 2, 'startTime': start, 'endTime': cursor})
            except Exception:
                break
            if not resp:
                break
            chunk = resp if isinstance(resp, list) else resp.get('rows') or resp.get('data') or []
            if not chunk:
                break
            for r in chunk:
                if (r.get('status') or '').upper() in ('SUCCESS', '') or r.get('status') == 1:
                    rows.append(r)
            if cursor - chunk_ms <= oldest:
                break
            cursor = start
        self._sub_transfer_history_cache[key] = (rows, time.time())
        return rows

    def net_injected_capital_usdt(self, lookback_days: int = 365) -> tuple[float | None, dict]:
        """Sum of completed USDT inflows − outflows over the lookback window:
            inflows  = external deposits + master→sub transfers (if sub-account)
            outflows = external withdrawals + sub→master transfers
        Returns ``(net, meta)`` where meta carries per-component count and total
        for the UI. Returns ``(None, {'error': ...})`` if every history endpoint
        came back empty so the caller falls back to manual CapitalFlow rows."""
        deps: list[dict] = []
        wdrs: list[dict] = []
        sub_in: list[dict] = []
        sub_out: list[dict] = []
        try:
            deps = self.deposit_history('USDT', lookback_days=lookback_days)
        except Exception:
            pass
        try:
            wdrs = self.withdrawal_history('USDT', lookback_days=lookback_days)
        except Exception:
            pass
        try:
            sub_in = self.sub_account_transfer_history('USDT', incoming=True, lookback_days=lookback_days)
        except Exception:
            pass
        try:
            sub_out = self.sub_account_transfer_history('USDT', incoming=False, lookback_days=lookback_days)
        except Exception:
            pass
        deps_total = sum(float(d.get('amount') or 0) for d in deps)
        wdrs_total = sum(float(w.get('amount') or 0) for w in wdrs)
        # Sub-account transfer rows use 'qty' historically and 'amount' in newer responses.
        sub_in_total = sum(float(r.get('qty') or r.get('amount') or 0) for r in sub_in)
        sub_out_total = sum(float(r.get('qty') or r.get('amount') or 0) for r in sub_out)
        net = (deps_total + sub_in_total) - (wdrs_total + sub_out_total)
        if not deps and not wdrs and not sub_in and not sub_out:
            return None, {'error': 'no SAPI history available (missing permission, sub-account keys, or no activity)'}
        return net, {
            'deposits_count': len(deps),
            'deposits_total': deps_total,
            'withdrawals_count': len(wdrs),
            'withdrawals_total': wdrs_total,
            'sub_in_count': len(sub_in),
            'sub_in_total': sub_in_total,
            'sub_out_count': len(sub_out),
            'sub_out_total': sub_out_total,
            'asset': 'USDT',
            'lookback_days': lookback_days,
        }


# ─── KuCoin gateway ─────────────────────────────────────────────────────────
# Implements the venue-specific overrides; everything else inherits from
# :class:`VenueGateway`. Notable differences vs Binance:
#   * Spot ↔ futures transfers go through KuCoin's ``innerTransfer`` SAPI on
#     the spot client (Binance uses ``sapiPostAssetTransfer``).
#   * Earn / Pool-X is deferred — KuCoin's lend product pays in the deposited
#     asset rather than USDT, which doesn't fit the bot's compounded-USDT
#     APY accounting. Inherit the no-op earn_* methods.
#   * Net-injected-capital history is venue-specific and not yet wired —
#     dashboard falls back to manual CapitalFlow rows on KuCoin until then.
#   * Symbol shape: ccxt normalises both KuCoin's perp suffix
#     (``XBTUSDTM`` → ``BTC/USDT:USDT``) and Binance's, so the bot's symbol
#     handling is unchanged.

class KuCoinGateway(VenueGateway):
    venue_id = 'kucoin'
    name = 'KuCoin'

    def __init__(self) -> None:
        super().__init__()
        common = {
            'apiKey': settings.kucoin_api_key,
            'secret': settings.kucoin_api_secret,
            'password': settings.kucoin_api_passphrase,
            'enableRateLimit': True,
        }
        self.spot = ccxt.kucoin(common)
        self.futures = ccxt.kucoinfutures(common)

    # ─── Spot ↔ futures transfer (KuCoin innerTransfer) ───────────────────

    def _inner_transfer(self, from_account: str, to_account: str, amount_usdt: float) -> tuple[bool, str]:
        # ccxt's KuCoin client has historically exposed innerTransfer under
        # different names. Probe a small ordered list and call the first that
        # exists — same pattern used for Binance SAPI lookups.
        for name in (
            'privatePostAccountsInnerTransfer',
            'private_post_accounts_inner_transfer',
            'privatePostAccountsUniversalTransfer',
        ):
            fn = getattr(self.spot, name, None)
            if callable(fn):
                try:
                    fn({
                        'currency': 'USDT',
                        'from': from_account,
                        'to': to_account,
                        'amount': f'{amount_usdt:.2f}',
                        'clientOid': f'autotrader-{int(time.time() * 1000)}',
                    })
                    return True, ''
                except Exception as e:
                    return False, str(e)
        return False, 'KuCoin innerTransfer not available in this ccxt build'

    def transfer_spot_to_futures(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        return self._inner_transfer('main', 'contract', amount_usdt)

    def transfer_futures_to_spot(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        return self._inner_transfer('contract', 'main', amount_usdt)


# ─── Multi-venue factory ────────────────────────────────────────────────────
# The bot calls :func:`make_gateways` to discover which venues are live.
# A venue is "live" iff its credentials are present in the environment.
# Order matters: gateways earlier in the list scan first each cycle, so they
# get first pick of capital and candidates. Binance leads by default — most
# mature integration, deepest liquidity.

def make_gateways() -> list[VenueGateway]:
    """Return the ordered list of currently-configured venue gateways. Empty
    list means no venue is configured — the bot loop becomes a no-op until
    credentials are set on the Configuration tab / env."""
    gws: list[VenueGateway] = []
    if settings.binance_api_key and settings.binance_api_secret:
        gws.append(BinanceGateway())
    if settings.kucoin_api_key and settings.kucoin_api_secret and settings.kucoin_api_passphrase:
        gws.append(KuCoinGateway())
    return gws
