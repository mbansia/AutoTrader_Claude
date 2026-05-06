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
from datetime import datetime, timedelta

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


def _ms_to_dt(value) -> datetime | None:
    """Convert a Binance/KuCoin millisecond-epoch timestamp (or ISO-8601
    string) to a UTC ``datetime``. Returns None for malformed input so the
    caller can drop the row."""
    if value is None:
        return None
    try:
        v = int(value)
        if v > 1e12:
            return datetime.utcfromtimestamp(v / 1000.0)
        if v > 1e9:
            return datetime.utcfromtimestamp(float(v))
    except (TypeError, ValueError):
        pass
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _row_hash(venue: str, kind: str, ts: datetime | None, amount: float) -> str:
    """Synthesize a stable id for a capital-flow row that the venue didn't
    return one for. Uniqueness is per (venue, kind, ts, rounded amount)."""
    ts_part = ts.isoformat() if ts else 'no-ts'
    return f'{venue}-{kind}-{ts_part}-{amount:.6f}'


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

    # Single dashboard render historically called ``safe_balances`` 5+ times
    # per venue (one in ``_current_equity``, one in ``equity_breakdown``,
    # one in ``_compute_equity_and_free``, etc.). On KuCoin's per-second
    # rate limits this triggered ``code 429000 "Too many requests"``. We
    # cache the latest result on the instance for ``BALANCE_CACHE_TTL``
    # seconds; the worker loop bypasses it explicitly when it needs fresh
    # numbers (e.g. after a transfer).
    BALANCE_CACHE_TTL = 10.0

    def __init__(self) -> None:
        # Subclasses must set these in their own __init__ before super().
        self.spot = None
        self.futures = None
        self.last_balance_error: str = ''
        # Per-endpoint error map populated by ``list_capital_flow_records``
        # so /monitoring can show which API endpoint refused (most common
        # cause: missing permission on the API key).
        self.last_history_errors: dict[str, str] = {}
        self._balance_cache: tuple[float, dict | None] | None = None

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

    def safe_balances(self, *, force_refresh: bool = False) -> dict | None:
        """Return ``{'spot': {...}, 'futures': {...}}`` from ccxt, or ``None`` on
        error (with the failure message pinned to ``self.last_balance_error``).

        Cached for ``BALANCE_CACHE_TTL`` seconds (see class attribute) — the
        dashboard renders multiple components that each ask for balances and
        we don't want to hammer per-second rate limits. Pass
        ``force_refresh=True`` after a transfer/order so the next read sees
        the post-action state."""
        if not force_refresh and self._balance_cache is not None:
            ts, cached = self._balance_cache
            if (time.time() - ts) < self.BALANCE_CACHE_TTL:
                return cached
        try:
            result = self._fetch_balances_uncached()
            self.last_balance_error = ''
            self._balance_cache = (time.time(), result)
            return result
        except Exception as e:
            self.last_balance_error = str(e)
            self._balance_cache = (time.time(), None)
            return None

    def _fetch_balances_uncached(self) -> dict:
        """Override hook — subclasses with custom wallet topology (e.g.
        KuCoin's ``trade`` vs ``main`` split) replace this. Default is the
        plain ccxt fetch_balance() pair."""
        return {'spot': self.spot.fetch_balance(), 'futures': self.futures.fetch_balance()}

    def invalidate_balance_cache(self) -> None:
        """Drop the in-memory balance cache so the next ``safe_balances``
        call hits the API. Used right after orders, transfers, and earn
        operations so downstream code sees the post-action state."""
        self._balance_cache = None

    # Per-(symbol, side) ticker cache. Equity composition iterates every
    # held base asset and called safe_price once each; on a venue with N
    # spot assets that's N rate-limited ticker calls per dashboard render.
    # KuCoin returned 429000 ("Too many requests") on this path. Cache
    # for the same TTL as balances so a single render hits ticker once
    # per symbol; the worker loop bypasses with force_refresh when it
    # needs a fresh price for a trade decision.
    PRICE_CACHE_TTL = 10.0

    def price(self, symbol: str) -> float:
        return float(self.spot.fetch_ticker(symbol)['last'])

    def perp_price(self, symbol: str) -> float:
        return float(self.futures.fetch_ticker(symbol)['last'])

    def safe_price(self, symbol: str, perp: bool = False, force_refresh: bool = False) -> float | None:
        if not hasattr(self, '_price_cache'):
            self._price_cache = {}  # type: ignore[attr-defined]
        cache_key = (symbol, perp)
        cached = self._price_cache.get(cache_key)  # type: ignore[attr-defined]
        if cached and not force_refresh:
            ts, value = cached
            if (time.time() - ts) < self.PRICE_CACHE_TTL:
                return value
        try:
            value = self.perp_price(symbol) if perp else self.price(symbol)
        except Exception:
            self._price_cache[cache_key] = (time.time(), None)  # type: ignore[attr-defined]
            return None
        self._price_cache[cache_key] = (time.time(), value)  # type: ignore[attr-defined]
        return value

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

    def funding_rates_dict(self) -> dict:
        """Return ``{symbol: {fundingRate, interval, fundingTimestamp,
        nextFundingTimestamp}, ...}`` for every USDT-perp on this venue.

        Default implementation uses ccxt's batch ``fetch_funding_rates()`` —
        works on Binance. Venues whose ccxt client can't batch (e.g. KuCoin
        futures, where ``fetch_funding_rates`` raises ``NotSupported``)
        override this to assemble the same shape from per-market info or
        per-symbol calls."""
        return self.futures.fetch_funding_rates()

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
            rates = self.funding_rates_dict()
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
            fill = self.spot.create_order(symbol, 'market', side, amount)
        else:
            fill = self.futures.create_order(symbol, 'market', side, amount)
        # Invalidate the balance cache so any caller that asks for
        # balances after an order sees the post-fill state, not stale data.
        self.invalidate_balance_cache()
        return fill

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

    def list_capital_flow_records(self, lookback_days: int = 365) -> list[dict]:
        """Return per-row capital movements over the lookback window so the bot
        can ingest them as ``CapitalFlow`` entries for XIRR and the per-flow
        timeline. Each row is ``{ts, amount, kind, external_id, note}``:
        * ``ts`` (datetime UTC), ``amount`` (USDT, positive=in, negative=out)
        * ``kind`` in ``deposit | withdrawal | sub_in | sub_out``
        * ``external_id`` is the venue's row id (used as the natural key so
          re-ingesting is idempotent — duplicates collapse)
        * ``note`` is a short description for the UI
        Default returns ``[]`` — venues without history simply ingest nothing."""
        return []


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
    # Earn subscribe is event-driven, not periodic — the bot only sweeps
    # when capital comes free (a position closes, a deposit lands, a
    # transfer settles). Those events are rare on a funding-arb strategy
    # (a handful per day max), so a default cooldown would just leave
    # idle USDT stranded. Defense in depth: if Binance actually returns
    # 77505 ("subscribed too many times"), we install a 24h punishment
    # cooldown for that asset and back off until the next day.
    EARN_SUBSCRIBE_DEFAULT_COOLDOWN_S = 0.0           # no proactive cooldown
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
        self.invalidate_balance_cache()
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
        self.invalidate_balance_cache()
        return bool(resp.get('success', True)), ''

    # ─── Binance universal transfer (spot ⇄ USDM-futures) ─────────────────

    def _universal_transfer(self, transfer_type: str, amount_usdt: float) -> tuple[bool, str]:
        for name in ('sapiPostAssetTransfer', 'sapi_post_asset_transfer'):
            fn = getattr(self.spot, name, None)
            if callable(fn):
                try:
                    fn({'type': transfer_type, 'asset': 'USDT', 'amount': f'{amount_usdt:.2f}'})
                except Exception as e:
                    return False, str(e)
                self.invalidate_balance_cache()
                return True, ''
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
        """Sum of completed USDT inflows − outflows over the lookback window.
        Single source of truth: delegate to :meth:`list_capital_flow_records`
        which already walks every Binance endpoint we know about (chain
        deposits / withdrawals + master↔sub transfers + universal-transfer
        history). Returns ``(net, meta)`` where ``meta`` carries the
        per-endpoint error map and per-kind row counts.

        Returns ``(None, meta)`` only when every endpoint raised — that's
        the signal for the caller to fall back to manual CapitalFlow rows.
        An endpoint that returned zero rows is still "API answered" and
        produces ``(0.0, meta)`` so the dashboard shows "0 USDT (no
        activity)" rather than "venue API not visible"."""
        rows = self.list_capital_flow_records(lookback_days=lookback_days)
        errors = dict(self.last_history_errors or {})
        if errors and not rows:
            # Distinguish "every endpoint failed" from "endpoints succeeded
            # with 0 rows". The historical contract: None means caller
            # should fall back to manual flows. Only fall back if at least
            # one error AND zero rows came back across the board.
            if all(k in errors for k in ('deposits', 'withdrawals', 'sub_in', 'sub_out')):
                return None, {'error': '; '.join(f'{k}: {v}' for k, v in errors.items())[:240]}
        counts: dict[str, int] = {}
        totals: dict[str, float] = {}
        net = 0.0
        for r in rows:
            kind = r.get('kind') or 'deposit'
            counts[kind] = counts.get(kind, 0) + 1
            totals[kind] = totals.get(kind, 0.0) + r.get('amount', 0.0)
            net += r.get('amount', 0.0)
        meta: dict = {
            'deposits_count': counts.get('deposit', 0),
            'deposits_total': totals.get('deposit', 0.0),
            'withdrawals_count': counts.get('withdrawal', 0),
            'withdrawals_total': totals.get('withdrawal', 0.0),
            'sub_in_count': counts.get('sub_in', 0),
            'sub_in_total': totals.get('sub_in', 0.0),
            'sub_out_count': counts.get('sub_out', 0),
            'sub_out_total': totals.get('sub_out', 0.0),
            'transfers_count': counts.get('transfer', 0),
            'transfers_total': totals.get('transfer', 0.0),
            'asset': 'USDT',
            'lookback_days': lookback_days,
        }
        if errors:
            meta['errors'] = errors
        return net, meta

    def list_capital_flow_records(self, lookback_days: int = 365) -> list[dict]:
        """Binance: external deposits, external withdrawals, master→sub
        transfer-in, sub→master transfer-out. Errors per endpoint are
        recorded on ``self.last_history_errors`` so /monitoring can surface
        which endpoint refused (typically a missing API permission)."""
        self.last_history_errors = {}
        rows: list[dict] = []
        try:
            for d in (self.deposit_history('USDT', lookback_days=lookback_days) or []):
                ts = _ms_to_dt(d.get('insertTime') or d.get('updateTime') or d.get('time'))
                amt = float(d.get('amount') or 0)
                ext = str(d.get('txId') or d.get('id') or '') or _row_hash('binance', 'deposit', ts, amt)
                rows.append({'ts': ts, 'amount': amt, 'kind': 'deposit', 'external_id': ext, 'note': 'Binance external deposit'})
        except Exception as e:
            self.last_history_errors['deposits'] = str(e)[:160]
        try:
            for w in (self.withdrawal_history('USDT', lookback_days=lookback_days) or []):
                ts = _ms_to_dt(w.get('applyTime') or w.get('completeTime') or w.get('time'))
                amt = -abs(float(w.get('amount') or 0))
                ext = str(w.get('id') or w.get('txId') or '') or _row_hash('binance', 'withdrawal', ts, amt)
                rows.append({'ts': ts, 'amount': amt, 'kind': 'withdrawal', 'external_id': ext, 'note': 'Binance external withdrawal'})
        except Exception as e:
            self.last_history_errors['withdrawals'] = str(e)[:160]
        try:
            for r in (self.sub_account_transfer_history('USDT', incoming=True, lookback_days=lookback_days, ttl_seconds=0) or []):
                ts = _ms_to_dt(r.get('time') or r.get('tranId'))
                amt = float(r.get('qty') or r.get('amount') or 0)
                ext = str(r.get('tranId') or r.get('id') or '') or _row_hash('binance', 'sub_in', ts, amt)
                rows.append({'ts': ts, 'amount': amt, 'kind': 'sub_in', 'external_id': ext, 'note': 'Master → sub transfer'})
        except Exception as e:
            self.last_history_errors['sub_in'] = str(e)[:160]
        try:
            for r in (self.sub_account_transfer_history('USDT', incoming=False, lookback_days=lookback_days, ttl_seconds=0) or []):
                ts = _ms_to_dt(r.get('time') or r.get('tranId'))
                amt = -abs(float(r.get('qty') or r.get('amount') or 0))
                ext = str(r.get('tranId') or r.get('id') or '') or _row_hash('binance', 'sub_out', ts, amt)
                rows.append({'ts': ts, 'amount': amt, 'kind': 'sub_out', 'external_id': ext, 'note': 'Sub → master transfer'})
        except Exception as e:
            self.last_history_errors['sub_out'] = str(e)[:160]
        # Universal transfer history (ccxt's fetch_transfers). For sub-account
        # API keys, master ↔ sub moves often surface here under types like
        # MAIN_FUNDING / FUNDING_MAIN (different envelope than the dedicated
        # sub-transfer endpoint). Folded in as ``transfer`` rows so we don't
        # double-count: when we see the same tranId in both sub_in/out and
        # here, the dedup-by-external_id keeps the first one.
        #
        # Binance caps the time-range parameter at 30 days on this endpoint
        # (errors with -5026 "Start time query records range is too large"
        # otherwise). We walk the full lookback in 30-day chunks; ccxt
        # threads ``since`` and ``params['endTime']`` straight through.
        since_ms = int((datetime.utcnow() - timedelta(days=lookback_days)).timestamp() * 1000)
        chunk_ms = 30 * 86400 * 1000
        end_ms = int(time.time() * 1000)
        cursor = end_ms
        transfer_rows: list[dict] = []
        while cursor > since_ms:
            window_start = max(since_ms, cursor - chunk_ms)
            try:
                chunk = self.spot.fetch_transfers('USDT', since=window_start, params={'endTime': cursor}) or []
                transfer_rows.extend(chunk)
            except Exception as e:
                # Save the most recent error so the operator can see what
                # went wrong; keep walking in case earlier windows succeed.
                self.last_history_errors['transfers'] = str(e)[:160]
                break
            if cursor - chunk_ms <= since_ms:
                break
            cursor = window_start
        for t in transfer_rows:
            ts = _ms_to_dt(t.get('timestamp'))
            raw_amt = float(t.get('amount') or 0)
            from_a = (t.get('fromAccount') or '').lower()
            to_a = (t.get('toAccount') or '').lower()
            # Direction inference using fromAccount/toAccount labels.
            signed = raw_amt
            if 'sub' in from_a and 'main' in to_a:
                signed = -abs(raw_amt)
            elif 'main' in to_a and 'funding' not in from_a and 'spot' not in from_a:
                signed = abs(raw_amt)
            ext = str(t.get('id') or '') or _row_hash('binance', 'transfer', ts, signed)
            rows.append({'ts': ts, 'amount': signed, 'kind': 'transfer', 'external_id': ext, 'note': f'Universal transfer {from_a}→{to_a}'})
        return [r for r in rows if r['ts'] is not None and abs(r['amount']) > 1e-9]


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

    # ccxt's kucoinfutures.fetch_funding_rates() raises NotSupported, so the
    # base-class scan would silently return zero candidates. KuCoin does
    # surface ``fundingFeeRate`` + ``nextFundingRateTime`` on every active
    # contract via ``/api/v1/contracts/active`` (which is what load_markets()
    # already calls). We rebuild the ccxt-shaped funding-rate dict from the
    # cached market info — no extra HTTP per scan.
    def funding_rates_dict(self) -> dict:
        if not self.futures.markets:
            self.futures.load_markets()
        out: dict = {}
        for symbol, market in self.futures.markets.items():
            if market.get('type') != 'swap' or market.get('quote') != 'USDT':
                continue
            info = market.get('info') or {}
            fr = info.get('fundingFeeRate')
            if fr is None:
                continue
            try:
                fr_val = float(fr)
            except (TypeError, ValueError):
                continue
            next_ms = info.get('nextFundingRateTime')  # ms-until-next on KuCoin
            try:
                next_ms = int(next_ms) if next_ms is not None else None
            except (TypeError, ValueError):
                next_ms = None
            now_ms = int(time.time() * 1000)
            next_ts = (now_ms + next_ms) if next_ms else None
            # KuCoin lists per-contract funding interval implicitly via
            # ``fundingRateSymbol`` (e.g. ".ETHUSDTMFPI8H"). Parse the trailing
            # "<n>H" if present, otherwise default to 8h (KuCoin's standard).
            interval_hours = 8.0
            frs = info.get('fundingRateSymbol') or ''
            if isinstance(frs, str) and frs.upper().endswith('H'):
                head = frs.rstrip('Hh')
                tail = ''
                for ch in reversed(head):
                    if ch.isdigit():
                        tail = ch + tail
                    else:
                        break
                if tail:
                    try:
                        interval_hours = float(tail)
                    except ValueError:
                        pass
            out[symbol] = {
                'fundingRate': fr_val,
                'interval': f'{int(interval_hours)}h',
                'fundingTimestamp': now_ms,
                'nextFundingTimestamp': next_ts,
            }
        return out

    # KuCoin separates cash across three wallet types:
    #   * ``trade``    — Trading Account (UI label); spot orders execute here.
    #   * ``main``     — Funding Account (UI label); deposits land here, and
    #                    KuCoin's auto-lend / Pool-X pulls idle USDT from
    #                    here. We treat this wallet as the "Earn" surface.
    #   * ``contract`` — Futures wallet; perp orders consume margin here.
    #
    # ccxt's default ``fetch_balance()`` only returns ``trade``. To match
    # the dashboard's three-bucket model (spot · earn · futures) we expose
    # ``trade`` as ``bals['spot']`` and the contract balance as
    # ``bals['futures']``. The funding-wallet balance is surfaced via
    # :meth:`earn_balance_usdt` so the equity total = spot + earn + futures
    # without double-counting.
    def _fetch_balances_uncached(self) -> dict:
        # Fetch all three wallets in one shot so the cache covers
        # spot + earn + futures with no extra round-trips. Stashes the main
        # wallet under ``earn`` so :meth:`earn_balance_usdt` reads it
        # without another API call.
        return {
            'spot': self.spot.fetch_balance({'type': 'trade'}),
            'earn': self.spot.fetch_balance({'type': 'main'}),
            'futures': self.futures.fetch_balance(),
        }

    def _main_wallet_usdt(self) -> tuple[float | None, str]:
        """Read the USDT balance of the ``main`` (Funding) wallet from the
        cached balance dict. Used by :meth:`earn_balance_usdt` since KuCoin's
        auto-lend operates on funding-wallet cash."""
        bals = self.safe_balances()
        if bals is None:
            return None, self.last_balance_error
        return float((bals.get('earn', {}).get('USDT') or {}).get('total') or 0), ''

    # ─── Earn-equivalent on KuCoin: park idle cash in the funding wallet ──
    # KuCoin doesn't expose a single "Simple Earn Flexible" endpoint that
    # behaves like Binance's. The clean equivalent is to keep idle USDT in
    # the ``main`` wallet so KuCoin's account-level auto-lend (the user
    # toggles this in the KuCoin UI) collects interest on it. The bot
    # therefore models earn_subscribe / earn_redeem as inner-transfers
    # between trade ↔ main; the displayed "Earn balance" is the main
    # wallet's USDT total. Cumulative yield is left at 0 because KuCoin
    # credits lend interest into the same main wallet — there's no
    # separate yield ledger to read without a per-day diff routine.
    def earn_balance_usdt(self) -> tuple[float | None, str]:
        return self._main_wallet_usdt()

    def earn_balance(self, asset: str = 'USDT') -> tuple[float | None, str]:
        if asset != 'USDT':
            return 0.0, ''  # only USDT auto-lend is wired
        return self._main_wallet_usdt()

    def earn_subscribe(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        return self._transfer('trade', 'main', amount_usdt)

    def earn_redeem(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        return self._transfer('main', 'trade', amount_usdt)

    # ─── Spot ↔ futures transfer (KuCoin) ────────────────────────────────
    # KuCoin splits transfers across two endpoints:
    #   * /api/v1/accounts/inner-transfer — main ↔ trade ↔ margin (spot
    #     wallets only). Rejects ``contract`` with code 400100
    #     "from not in the given range".
    #   * /api/v3/accounts/universal-transfer — handles everything,
    #     including ``contract`` ↔ ``trade`` and master ↔ sub.
    # ccxt's unified ``transfer()`` picks the right one based on whether
    # the route involves a non-spot wallet, so we always go through it.
    def _transfer(self, from_account: str, to_account: str, amount_usdt: float) -> tuple[bool, str]:
        try:
            self.spot.transfer('USDT', float(amount_usdt), from_account, to_account)
        except Exception as e:
            return False, str(e)
        self.invalidate_balance_cache()
        return True, ''

    # KuCoin's spot orders execute against the ``trade`` wallet (UI label
    # "Trading Account"); ``main`` is the funding wallet (deposits land here).
    # ``contract`` is the futures wallet. Spot↔futures transfers therefore
    # move trade↔contract; idle cash is parked in main (so Pool-X / earn
    # subscribe sees it). The bot calls ``stage_for_spot()`` before placing a
    # spot order to guarantee the trade wallet is funded.
    def transfer_spot_to_futures(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        return self._transfer('trade', 'contract', amount_usdt)

    def transfer_futures_to_spot(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        return self._transfer('contract', 'trade', amount_usdt)

    # ─── Capital-injection history (KuCoin) ───────────────────────────────
    # KuCoin sub-accounts: master→sub transfers come in via the
    # ``/api/v1/accounts/sub-transfer`` audit endpoint, and external
    # deposits via ``fetch_deposits``. ccxt exposes both. We sum USDT
    # inflows minus outflows over the lookback. Returns ``(None, meta)`` if
    # no endpoint returned anything, so the caller falls back to manual
    # CapitalFlow rows.
    def net_injected_capital_usdt(self, lookback_days: int = 365) -> tuple[float | None, dict]:
        since_ms = int((datetime.utcnow() - timedelta(days=lookback_days)).timestamp() * 1000)
        deps: list[dict] = []
        wdrs: list[dict] = []
        sub_xfers: list[dict] = []
        try:
            deps = self.spot.fetch_deposits('USDT', since=since_ms) or []
        except Exception:
            pass
        try:
            wdrs = self.spot.fetch_withdrawals('USDT', since=since_ms) or []
        except Exception:
            pass
        # Sub-account transfer history — endpoint name varies by ccxt vintage.
        for name in (
            'privateGetSubTransferRecord',
            'private_get_sub_transfer_record',
            'privateGetAccountsSubTransfer',
        ):
            fn = getattr(self.spot, name, None)
            if callable(fn):
                try:
                    resp = fn({'currency': 'USDT', 'startAt': since_ms})
                    sub_xfers = ((resp or {}).get('data') or {}).get('items') or []
                except Exception:
                    sub_xfers = []
                break
        deps_total = sum(float(d.get('amount') or 0) for d in deps)
        wdrs_total = sum(float(w.get('amount') or 0) for w in wdrs)
        # Sub-transfer rows carry direction='in'/'out' and amount.
        sub_in_total = sum(float(r.get('amount') or 0) for r in sub_xfers if r.get('direction') == 'in')
        sub_out_total = sum(float(r.get('amount') or 0) for r in sub_xfers if r.get('direction') == 'out')
        if not deps and not wdrs and not sub_xfers:
            return None, {'error': 'no KuCoin deposit/withdrawal/sub-transfer history (missing General permission, sub-account isolation, or no activity)'}
        net = (deps_total + sub_in_total) - (wdrs_total + sub_out_total)
        return net, {
            'deposits_count': len(deps),
            'deposits_total': deps_total,
            'withdrawals_count': len(wdrs),
            'withdrawals_total': wdrs_total,
            'sub_in_count': sum(1 for r in sub_xfers if r.get('direction') == 'in'),
            'sub_in_total': sub_in_total,
            'sub_out_count': sum(1 for r in sub_xfers if r.get('direction') == 'out'),
            'sub_out_total': sub_out_total,
            'asset': 'USDT',
            'lookback_days': lookback_days,
        }

    def list_capital_flow_records(self, lookback_days: int = 365) -> list[dict]:
        self.last_history_errors = {}
        since_ms = int((datetime.utcnow() - timedelta(days=lookback_days)).timestamp() * 1000)
        rows: list[dict] = []
        try:
            for d in (self.spot.fetch_deposits('USDT', since=since_ms) or []):
                ts = _ms_to_dt(d.get('timestamp'))
                amt = float(d.get('amount') or 0)
                ext = str(d.get('id') or d.get('txid') or '') or _row_hash('kucoin', 'deposit', ts, amt)
                rows.append({'ts': ts, 'amount': amt, 'kind': 'deposit', 'external_id': ext, 'note': 'KuCoin external deposit'})
        except Exception as e:
            self.last_history_errors['deposits'] = str(e)[:160]
        try:
            for w in (self.spot.fetch_withdrawals('USDT', since=since_ms) or []):
                ts = _ms_to_dt(w.get('timestamp'))
                amt = -abs(float(w.get('amount') or 0))
                ext = str(w.get('id') or w.get('txid') or '') or _row_hash('kucoin', 'withdrawal', ts, amt)
                rows.append({'ts': ts, 'amount': amt, 'kind': 'withdrawal', 'external_id': ext, 'note': 'KuCoin external withdrawal'})
        except Exception as e:
            self.last_history_errors['withdrawals'] = str(e)[:160]
        # Sub-account transfer audit (master ↔ sub). KuCoin's ccxt client
        # exposes the underlying SAPI under multiple possible names depending
        # on vintage; probe an ordered list and stop on the first one that
        # actually responds (success OR a real API error). If none exist, we
        # log a cause so /monitoring shows what to fix.
        # Inter-account transfer audit. KuCoin reorganised this surface
        # several times; ccxt's current build exposes ``privateGetTransferList``
        # (the v1 unified transfer history) which surfaces master ↔ sub
        # moves to a sub-account API key. We probe an ordered list and use
        # the first that's actually a callable on the client.
        sub_endpoint_used = None
        for name in (
            'privateGetTransferList',               # v1 transfer history (current ccxt)
            'private_get_transfer_list',
            'privateGetSubTransferRecord',          # legacy
            'private_get_sub_transfer_record',
            'privateGetAccountsSubTransfer',
            'futuresPrivateGetSubTransferRecord',
        ):
            fn = getattr(self.spot, name, None)
            if not callable(fn):
                continue
            sub_endpoint_used = name
            try:
                resp = fn({'currency': 'USDT', 'startAt': since_ms})
                items = ((resp or {}).get('data') or {}).get('items') or []
            except Exception as e:
                self.last_history_errors['sub_transfer'] = f'{name}: {str(e)[:140]}'
                items = []
            for r in items:
                # KuCoin transfer-list rows can carry either ``direction``
                # ('in'/'out') or ``type`` ('IN'/'OUT'/'INNER'); try both.
                direction = (r.get('direction') or r.get('type') or '').lower()
                ts = _ms_to_dt(r.get('createdAt') or r.get('time'))
                amt_raw = float(r.get('amount') or 0)
                kind = 'sub_in' if direction == 'in' else ('sub_out' if direction == 'out' else 'transfer')
                signed = amt_raw if direction == 'in' else (-abs(amt_raw) if direction == 'out' else amt_raw)
                ext = str(r.get('id') or r.get('bizNo') or '') or _row_hash('kucoin', kind, ts, signed)
                rows.append({'ts': ts, 'amount': signed, 'kind': kind, 'external_id': ext, 'note': f'KuCoin {sub_endpoint_used} ({direction or "inner"})'})
            break
        if sub_endpoint_used is None:
            self.last_history_errors['sub_transfer'] = 'no sub-transfer endpoint exposed by this ccxt build'
        # ccxt's unified fetch_transfers — KuCoin's universal-transfer log
        # surfaces master ↔ sub moves and inner-account moves. Cheap
        # fallback if the bespoke sub-transfer endpoints don't exist or
        # were rejected by the API key.
        try:
            for t in (self.spot.fetch_transfers('USDT', since=since_ms) or []):
                ts = _ms_to_dt(t.get('timestamp'))
                raw_amt = float(t.get('amount') or 0)
                from_a = (t.get('fromAccount') or '').lower()
                to_a = (t.get('toAccount') or '').lower()
                # KuCoin labels: 'main' / 'trade' / 'contract' / 'sub'.
                # A row whose toAccount is on this account = inflow.
                signed = abs(raw_amt) if 'sub' not in to_a else -abs(raw_amt)
                ext = str(t.get('id') or '') or _row_hash('kucoin', 'transfer', ts, signed)
                rows.append({'ts': ts, 'amount': signed, 'kind': 'transfer', 'external_id': ext, 'note': f'Universal transfer {from_a}→{to_a}'})
        except Exception as e:
            self.last_history_errors['transfers'] = str(e)[:160]
        return [r for r in rows if r['ts'] is not None and abs(r['amount']) > 1e-9]


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
