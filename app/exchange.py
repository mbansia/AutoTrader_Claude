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
    # Bumped from 10s to 30s after persistent KuCoin 429000 errors. The
    # tradeoff: dashboard balances may be up to 30s stale right after a
    # trade. That's acceptable because (a) the cycle loop only runs every
    # 30s anyway so the worker reads ARE fresh per cycle, and (b) on a
    # delta-neutral funding arb, 30s of stale balance is irrelevant for
    # decision-making. force_refresh=True still bypasses the cache for
    # the worker's own pre-trade reads.
    BALANCE_CACHE_TTL = 30.0
    # Rate-limit recovery window. When a venue returns 429 we install a
    # pause until ``_rate_limit_pause_until`` and skip API calls during
    # that window — cached data is returned instead. The pause grows
    # exponentially on repeated 429s (60s → 120s → 240s, capped at 600s)
    # and resets after a successful call. This lets KuCoin's per-user
    # rate-limit window decay without us hammering it.
    RATE_LIMIT_PAUSE_INITIAL_S = 60.0
    RATE_LIMIT_PAUSE_MAX_S = 600.0

    def __init__(self) -> None:
        # Subclasses must set these in their own __init__ before super().
        self.spot = None
        self.futures = None
        self.last_balance_error: str = ''
        self.last_earn_breakdown: dict = {}
        # Per-endpoint error map populated by ``list_capital_flow_records``
        # so /monitoring can show which API endpoint refused (most common
        # cause: missing permission on the API key).
        self.last_history_errors: dict[str, str] = {}
        self._balance_cache: tuple[float, dict | None] | None = None
        # Rate-limit recovery state.
        self._rate_limit_pause_until: float = 0.0
        self._rate_limit_consecutive: int = 0

    def is_rate_limited(self) -> bool:
        return time.time() < self._rate_limit_pause_until

    def _note_rate_limit(self, raw_err: str) -> None:
        """Mark this gateway as rate-limited. Pause grows exponentially
        on consecutive hits and resets when a non-429 call succeeds."""
        self._rate_limit_consecutive += 1
        pause = min(
            self.RATE_LIMIT_PAUSE_MAX_S,
            self.RATE_LIMIT_PAUSE_INITIAL_S * (2 ** (self._rate_limit_consecutive - 1)),
        )
        self._rate_limit_pause_until = time.time() + pause
        self.last_balance_error = f'rate-limited ({pause:.0f}s pause): {raw_err[:140]}'

    def _note_request_ok(self) -> None:
        """Reset the consecutive-hit counter when a call succeeds."""
        self._rate_limit_consecutive = 0

    @staticmethod
    def _is_rate_limit_error(err: Exception) -> bool:
        s = str(err).lower()
        return ('429' in s or 'too many requests' in s or 'rate limit' in s
                or '"code":"429000"' in s or 'user-level rate limit' in s)

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
        the post-action state. While the gateway is in a rate-limit pause
        (``is_rate_limited()`` True) we always return the last cached value
        regardless of TTL — calling the API again would just extend the
        ban. ``last_balance_error`` carries a "rate-limited (Ns pause)"
        message during the window so the UI surfaces what's happening."""
        # If we're rate-limited, never re-hit the API. Return whatever was
        # last cached (even None), so callers see the pause and stale data
        # rather than triggering further bans.
        if self.is_rate_limited() and self._balance_cache is not None:
            return self._balance_cache[1]
        if not force_refresh and self._balance_cache is not None:
            ts, cached = self._balance_cache
            if (time.time() - ts) < self.BALANCE_CACHE_TTL:
                return cached
        try:
            result = self._fetch_balances_uncached()
            self.last_balance_error = ''
            self._balance_cache = (time.time(), result)
            self._note_request_ok()
            return result
        except Exception as e:
            if self._is_rate_limit_error(e):
                self._note_rate_limit(str(e))
                # Keep the previous cache instead of nulling — stale data
                # is more useful than nothing while we wait.
                return self._balance_cache[1] if self._balance_cache else None
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
    # Same 30s ceiling as balance cache. Spot/perp prices for the
    # dashboard's equity composition don't need real-time precision —
    # the bot's pre-trade reads always use force_refresh=True for
    # actual order pricing.
    PRICE_CACHE_TTL = 30.0

    def price(self, symbol: str) -> float:
        return float(self.spot.fetch_ticker(symbol)['last'])

    def perp_price(self, symbol: str) -> float:
        return float(self.futures.fetch_ticker(symbol)['last'])

    def safe_price(self, symbol: str, perp: bool = False, force_refresh: bool = False) -> float | None:
        if not hasattr(self, '_price_cache'):
            self._price_cache = {}  # type: ignore[attr-defined]
        cache_key = (symbol, perp)
        cached = self._price_cache.get(cache_key)  # type: ignore[attr-defined]
        # Honour the rate-limit pause: return last cached value (even
        # if stale) instead of triggering further bans.
        if self.is_rate_limited() and cached:
            return cached[1]
        if cached and not force_refresh:
            ts, value = cached
            if (time.time() - ts) < self.PRICE_CACHE_TTL:
                return value
        try:
            value = self.perp_price(symbol) if perp else self.price(symbol)
            self._note_request_ok()
        except Exception as e:
            if self._is_rate_limit_error(e):
                self._note_rate_limit(str(e))
                # Use last cached value if we have one; else None.
                return cached[1] if cached else None
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
        # invalidate_balance_cache dropped — 30s TTL is short enough; eager invalidation triggered KuCoin 429s
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

    def net_injected_capital_usdt(self, lookback_days: int = 10) -> tuple[float | None, dict]:
        """Returns ``(net, meta)`` where ``net`` is in USDT and ``meta`` carries
        a per-component breakdown for the UI. ``None`` signals the caller to
        fall back to manual CapitalFlow rows."""
        return None, {'error': f'history not wired on {self.name}'}

    def list_capital_flow_records(self, lookback_days: int = 10) -> list[dict]:
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

    def account_type(self) -> tuple[str, str]:
        """Read the account type live from the venue's API. Returns
        ``(label, detail)`` where ``label`` is the human-readable name
        (e.g. "Portfolio Margin", "Classic", "UTA") and ``detail`` is a
        short raw string from the API (e.g. account-mode field) for the
        operator to verify against the venue's own UI. Default returns
        ``('Unknown', 'no probe wired')`` — venue subclasses override."""
        return 'Unknown', 'no probe wired'

    def equity_buckets(self) -> list[dict]:
        """Return per-bucket equity items for the dashboard donut, with
        venue-correct labels (e.g. ``Binance · PM USDT`` and
        ``Binance · BFUSD`` rather than the legacy
        ``spot / futures / earn`` triple — those don't apply under PM
        or UTA, where the account is a single unified pool plus
        optional yield-bearing collateral).
        Each item: ``{label, value, color, venue}``. ``value`` is in
        USDT-equivalent. Default implementation falls back to the
        classic three-bucket breakdown by reading ``safe_balances`` and
        ``earn_balance_usdt``; venue subclasses with non-classic
        account types override to use their proper terminology."""
        bals = self.safe_balances() or {}
        items: list[dict] = []
        spot_usdt = float((bals.get('spot', {}).get('USDT') or {}).get('total') or 0)
        fut_usdt = float((bals.get('futures', {}).get('USDT') or {}).get('total') or 0)
        if spot_usdt > 0:
            items.append({'label': f'{self.name} · Spot USDT', 'value': spot_usdt, 'venue': self.venue_id, 'color': '#38bdf8'})
        if fut_usdt > 0:
            items.append({'label': f'{self.name} · Futures USDT', 'value': fut_usdt, 'venue': self.venue_id, 'color': '#fbbf24'})
        spot_assets_value = 0.0
        META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
        for asset, bal in (bals.get('spot') or {}).items():
            if asset in META_KEYS or asset == 'USDT' or not isinstance(bal, dict):
                continue
            qty = float(bal.get('total') or 0)
            if qty <= 0:
                continue
            px = self.safe_price(f'{asset}/USDT') or 0
            spot_assets_value += qty * px
        if spot_assets_value > 0:
            items.append({'label': f'{self.name} · Spot assets', 'value': spot_assets_value, 'venue': self.venue_id, 'color': '#818cf8'})
        try:
            earn_usdt, _ = self.earn_balance_usdt()
            earn_usdt = earn_usdt or 0.0
        except Exception:
            earn_usdt = 0.0
        if earn_usdt > 0:
            items.append({'label': f'{self.name} · Earn', 'value': earn_usdt, 'venue': self.venue_id, 'color': '#4ade80'})
        return items


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
        # invalidate_balance_cache dropped — 30s TTL is short enough; eager invalidation triggered KuCoin 429s
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
        # invalidate_balance_cache dropped — 30s TTL is short enough; eager invalidation triggered KuCoin 429s
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
                # invalidate_balance_cache dropped — see VenueGateway.safe_balances comment
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

    def deposit_history(self, asset: str = 'USDT', lookback_days: int = 10, ttl_seconds: float = 300.0) -> list[dict]:
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

    def withdrawal_history(self, asset: str = 'USDT', lookback_days: int = 10, ttl_seconds: float = 300.0) -> list[dict]:
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

    def sub_account_transfer_history(self, asset: str = 'USDT', incoming: bool = True, lookback_days: int = 10, ttl_seconds: float = 300.0) -> list[dict]:
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

    def net_injected_capital_usdt(self, lookback_days: int = 10) -> tuple[float | None, dict]:
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

    def list_capital_flow_records(self, lookback_days: int = 10) -> list[dict]:
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
            # Filter out intra-account moves — these are the bot's own
            # spot↔futures shuttle, NOT capital flowing in/out of the
            # account. The user's $30 deposit was master→sub via
            # /sapi/v1/sub-account/sub/transfer/history (separate
            # endpoint), so anything here that's intra-account is just
            # noise polluting Net Injected Capital.
            INTRA_ACCOUNT = {
                'spot', 'main', 'funding', 'mining', 'margin',
                'cross_margin', 'isolated_margin', 'isolatedmargin', 'crossmargin',
                'linear', 'inverse', 'swap', 'umfuture', 'cmfuture',
                'pm', 'portfolio_margin', 'portfoliomargin',
            }
            if from_a in INTRA_ACCOUNT and to_a in INTRA_ACCOUNT:
                continue
            # Direction inference using fromAccount/toAccount labels.
            signed = raw_amt
            if 'sub' in from_a and 'main' in to_a:
                signed = -abs(raw_amt)
            elif 'main' in to_a and 'funding' not in from_a and 'spot' not in from_a:
                signed = abs(raw_amt)
            ext = str(t.get('id') or '') or _row_hash('binance', 'transfer', ts, signed)
            rows.append({'ts': ts, 'amount': signed, 'kind': 'transfer', 'external_id': ext, 'note': f'Universal transfer {from_a}→{to_a}'})
        return [r for r in rows if r['ts'] is not None and abs(r['amount']) > 1e-9]

    # ─── Portfolio Margin overrides (active when account is in PM mode) ────
    # Binance Portfolio Margin replaces the Classic spot/futures/earn split
    # with a unified margin pool that holds USDT, BFUSD (yield-bearing
    # USDT-pegged), USDC, and other margin assets simultaneously. Orders
    # route through ``/papi/v1/*`` (separate from ``/api/v3/*`` and
    # ``/fapi/v1/*``); calling Classic endpoints on a PM account returns
    # -2015. The user has confirmed the sub-account is in PM mode, so we
    # always go through these paths — no Classic fallback to maintain.
    #
    # Endpoint references (Binance API docs, Sept 2024 revision):
    #   /papi/v1/balance           — unified balance per asset
    #   /papi/v1/account           — overall account / margin state
    #   /papi/v1/um/order          — USDM-perp order placement
    #   /papi/v1/um/leverage       — set leverage on a UM symbol
    #   /papi/v1/margin/order      — cross-margin spot order placement
    #   /papi/v1/um/positionRisk   — open UM positions
    #   /papi/v1/asset-collection  — collect margin from one PM bucket to another
    #
    # We don't directly subscribe / redeem BFUSD via API — Binance's
    # auto-collection feature (toggled in the Binance UI per asset) does
    # that automatically as USDT lands in the margin pool. The bot reads
    # the BFUSD balance and surfaces it as the "Earn" bucket on the
    # dashboard. ``earn_subscribe`` is a no-op under PM with a clear log
    # line; the user enables auto-collection once and walks away.

    @staticmethod
    def _papi(client, candidates: tuple[str, ...]):
        """Probe a small list of ccxt method names for a /papi/ endpoint;
        return the first callable. ccxt sometimes flips between ``papiPost…``
        and ``papi_post_…`` naming across versions."""
        for name in candidates:
            fn = getattr(client, name, None)
            if callable(fn):
                return fn
        return None

    def account_type(self) -> tuple[str, str]:
        """Read Binance's account type live. Calls /papi/v1/account first
        (only succeeds on PM accounts) — if that returns 200, account is
        in Portfolio Margin. Falls back to /sapi/v1/account/apiTradingStatus
        which works on Classic too. Returns (label, detail) for /monitoring."""
        fn = self._papi(self.spot, ('papiGetAccount', 'papi_get_account'))
        if fn is not None:
            try:
                resp = fn({})
                if isinstance(resp, dict) and resp:
                    detail = str(resp.get('accountType') or resp.get('actualEquity') or 'PM account')
                    return 'Portfolio Margin', detail[:80]
            except Exception as e:
                msg = str(e).lower()
                if '-2015' in msg or 'invalid api-key' in msg or 'permission' in msg:
                    # PM endpoint refused — could be Classic OR a permissions
                    # issue. Probe a Classic-only endpoint to disambiguate.
                    pass
                else:
                    return 'Portfolio Margin (probe error)', str(e)[:80]
        # Fall back to Classic detection: /sapi/v1/account/apiTradingStatus
        # works on Classic accounts; -2015 here means the key really is bad.
        try:
            resp = self._call_sapi((
                'sapiV1GetAccountApiTradingStatus',
                'sapi_v1_get_account_api_trading_status',
            ), {})
            if resp:
                return 'Classic', 'spot + futures separate (no PM)'
        except Exception as e:
            return 'Unknown', f'both probes failed: {str(e)[:80]}'
        return 'Unknown', 'PM probe rejected, Classic probe returned nothing'

    def equity_buckets(self) -> list[dict]:
        """PM-correct equity buckets for Binance. Replaces the Classic
        spot/futures/earn triple with the actual PM concepts:
          * ``Binance · PM USDT``        — USDT in the unified margin pool
          * ``Binance · BFUSD``          — yield-bearing PM collateral
          * ``Binance · PM collateral``  — non-USDT/BFUSD assets in the
                                            pool (e.g. ETH from a long leg)
          * ``Binance · Classic Spot``   — legacy spot wallet (separate
                                            from PM; gets populated by
                                            Simple Earn redeems and
                                            external deposits)
          * ``Binance · Simple Earn``    — legacy Simple Earn flexible
                                            position (still a real
                                            balance until the user
                                            mints BFUSD from it)
        Buckets with zero balance are omitted so the donut/legend
        stays tight."""
        bals = self.safe_balances() or {}
        items: list[dict] = []
        # PM unified pool (synthesised into 'spot' by _fetch_balances_uncached;
        # 'futures' is the same number under PM). Show as a single PM USDT
        # bucket — naming the same number twice would be confusing.
        pm_usdt = float((bals.get('spot', {}).get('USDT') or {}).get('total') or 0)
        if pm_usdt > 0:
            items.append({'label': f'{self.name} · PM USDT', 'value': pm_usdt, 'venue': self.venue_id, 'color': '#38bdf8'})
        # BFUSD (the yield-bearing margin asset). _fetch_balances_uncached
        # surfaces it in 'earn' for compatibility with downstream readers.
        bfusd = float((bals.get('earn', {}).get('USDT') or {}).get('total') or 0)
        if bfusd > 0:
            items.append({'label': f'{self.name} · BFUSD', 'value': bfusd, 'venue': self.venue_id, 'color': '#4ade80'})
        # Non-USDT collateral (e.g. ETH held as the long spot leg of an arb).
        collateral_value = 0.0
        META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
        for asset, bal in (bals.get('spot') or {}).items():
            if asset in META_KEYS or asset == 'USDT' or not isinstance(bal, dict):
                continue
            qty = float(bal.get('total') or 0)
            if qty <= 0:
                continue
            px = self.safe_price(f'{asset}/USDT') or 0
            collateral_value += qty * px
        if collateral_value > 0:
            items.append({'label': f'{self.name} · PM collateral assets', 'value': collateral_value, 'venue': self.venue_id, 'color': '#818cf8'})
        # Legacy Simple Earn USDT — still a real balance until the user
        # mints BFUSD from it. earn_balance_usdt() aggregates BFUSD +
        # Simple Earn; we already counted BFUSD above, so subtract.
        try:
            earn_total, _ = self.earn_balance_usdt()
            simple_earn = max(0.0, (earn_total or 0.0) - bfusd)
        except Exception:
            simple_earn = 0.0
        if simple_earn > 0.01:
            items.append({'label': f'{self.name} · Simple Earn (legacy)', 'value': simple_earn, 'venue': self.venue_id, 'color': '#facc15'})
        return items

    def _fetch_balances_uncached(self) -> dict:
        """Read PM unified balance + classic Spot wallet, then synthesise
        the bot's three-bucket shape (spot · earn · futures). Two reads
        because PM and classic Spot are SEPARATE wallets even on a PM
        account: Simple Earn redeems land in classic Spot, deposits via
        the spot deposit address land in classic Spot, and only an
        explicit Wallet→Cross Margin transfer (or Binance's auto-
        collection) moves them into the PM pool. We read both and sum
        so neither balance hides from the dashboard.

        Buckets:
        * ``spot.USDT``    = PM-pool USDT + classic Spot USDT
        * ``earn.USDT``    = BFUSD balance (yield-bearing PM collateral)
        * ``futures.USDT`` = mirrors spot (PM unifies; both can fund a perp)
        Per-base-asset spot holdings (long leg of an open arb) are read
        from PM-pool first; classic spot adds on if PM didn't surface it."""
        fn = self._papi(self.spot, ('papiGetBalance', 'papi_get_balance'))
        if fn is None:
            # Fall back to classic ccxt fetch_balance — bot will still see
            # SOMETHING, and the dashboard banner will surface the missing
            # endpoint. Should never happen on a current ccxt build.
            return {'spot': self.spot.fetch_balance(), 'futures': self.futures.fetch_balance()}
        rows = fn({})
        if isinstance(rows, dict):
            rows = rows.get('data') or rows.get('balances') or []
        usdt_total = usdt_free = bfusd_total = 0.0
        per_asset: dict[str, dict] = {}
        for r in rows:
            asset = r.get('asset') or r.get('currency') or ''
            try:
                # ``crossMarginFree`` is the actually-deployable cross-margin
                # quantity; ``totalWalletBalance`` is total holdings across
                # margin + UM. Different ccxt builds expose different field
                # names — try the obvious ones in order.
                free = float(r.get('crossMarginFree') or r.get('umWalletBalance') or r.get('free') or 0)
                total = float(r.get('totalWalletBalance') or r.get('crossMarginAsset') or r.get('total') or free)
            except (TypeError, ValueError):
                continue
            if asset == 'USDT':
                usdt_total += total
                usdt_free += free
            elif asset == 'BFUSD':
                bfusd_total += total
            elif total > 0:
                per_asset[asset] = {'free': free, 'used': max(0.0, total - free), 'total': total}
        # Classic Spot wallet — read separately and merge. This catches the
        # USDT that lands here after a Simple Earn redeem, an external
        # deposit, or any other path that doesn't land directly in the PM
        # pool. Without this read the dashboard would show 0 for $30+ of
        # cash that's clearly visible in the Binance UI.
        try:
            spot_classic = self.spot.fetch_balance()
            classic_usdt = float((spot_classic.get('USDT') or {}).get('total') or 0)
            classic_usdt_free = float((spot_classic.get('USDT') or {}).get('free') or 0)
            usdt_total += classic_usdt
            usdt_free += classic_usdt_free
            META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
            for asset, bal in spot_classic.items():
                if asset in META_KEYS or asset == 'USDT' or not isinstance(bal, dict):
                    continue
                qty = float(bal.get('total') or 0)
                if qty <= 0:
                    continue
                # Prefer PM-pool record if both have the same asset.
                if asset not in per_asset:
                    per_asset[asset] = {'free': float(bal.get('free') or 0), 'used': float(bal.get('used') or 0), 'total': qty}
        except Exception:
            # Classic spot read isn't critical — if it errors (rate limit,
            # permission), PM-pool numbers are still correct.
            pass
        spot = {'USDT': {'free': usdt_free, 'used': max(0.0, usdt_total - usdt_free), 'total': usdt_total},
                **per_asset,
                'free': {'USDT': usdt_free, **{k: v['free'] for k, v in per_asset.items()}},
                'used': {'USDT': max(0.0, usdt_total - usdt_free), **{k: v['used'] for k, v in per_asset.items()}},
                'total': {'USDT': usdt_total, **{k: v['total'] for k, v in per_asset.items()}}}
        # Futures bucket mirrors spot under PM — the bot's downstream code
        # reads bals['futures']['USDT'] for margin checks; keeping it equal
        # to spot reflects the unified-margin reality.
        # Under PM there is no separate futures wallet — the pool funds
        # both spot-margin and UM-perp legs. We expose ``futures.USDT.free``
        # as the pool's free amount so the bot's trade-sizing logic
        # (which reads ``min(spot_free, fut_free)``) still works, but
        # ``futures.USDT.total`` is 0 so the equity-sum loop
        # (``spot_total + fut_total + ...``) doesn't double-count the
        # same pool USDT.
        futures = {'USDT': {'free': usdt_free, 'used': 0.0, 'total': 0.0},
                   'free': {'USDT': usdt_free}, 'used': {'USDT': 0.0},
                   'total': {'USDT': 0.0}}
        earn = {'USDT': {'free': bfusd_total, 'used': 0.0, 'total': bfusd_total},
                'free': {'USDT': bfusd_total}, 'used': {'USDT': 0.0}, 'total': {'USDT': bfusd_total}}
        return {'spot': spot, 'earn': earn, 'futures': futures}

    def earn_balance_usdt(self) -> tuple[float | None, str]:
        """Aggregate Binance's 'yield-bearing' surfaces.
        Under PM the canonical earn asset is BFUSD (cross-collateral margin
        that earns yield). But many users — including ours after the migration
        — still hold legacy Simple Earn Flexible USDT positions that the bot
        was managing pre-PM. Both should count toward equity until the user
        migrates everything via the dashboard's 'Migrate Simple Earn → PM'
        button (see _migrate_simple_earn_to_pm in main.py).

        Returns the sum: BFUSD (from cached PM balance) + Simple Earn
        flexible USDT position. Either component may be 0 — that's fine."""
        bals = self.safe_balances()
        bfusd_total = 0.0
        if bals is not None:
            bfusd_total = float((bals.get('earn', {}).get('USDT') or {}).get('total') or 0)
        # Simple Earn flexible USDT — works on both Classic AND PM accounts
        # (it's a separate Simple Earn product, not linked to the trading
        # account type).
        simple_earn_total = 0.0
        try:
            resp = self._call_sapi((
                'sapiV1GetSimpleEarnFlexiblePosition',
                'sapi_v1_get_simple_earn_flexible_position',
                'sapiGetSimpleEarnFlexiblePosition',
            ), {'asset': 'USDT'})
            if resp:
                rows = resp.get('rows') or resp.get('data') or []
                for r in rows:
                    if r.get('asset') == 'USDT':
                        try:
                            simple_earn_total += float(r.get('totalAmount') or r.get('amount') or 0)
                        except (TypeError, ValueError):
                            pass
        except Exception:
            pass  # Simple Earn endpoint may be 4xx on some PM accounts;
                  # that's fine, balance falls back to BFUSD-only.
        total = bfusd_total + simple_earn_total
        # Surface the breakdown via last_earn_breakdown so the dashboard
        # can show "BFUSD X.XX + Simple Earn Y.YY" without a second call.
        self.last_earn_breakdown = {'bfusd': bfusd_total, 'simple_earn_flexible_usdt': simple_earn_total}
        return total, ''

    def earn_balance(self, asset: str = 'USDT') -> tuple[float | None, str]:
        if asset != 'USDT':
            return 0.0, ''  # only BFUSD (USDT-pegged) is wired
        return self.earn_balance_usdt()

    def earn_subscribe(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        """Mint BFUSD from idle USDT under Portfolio Margin. Calls
        ``/sapi/v1/portfolio/mint`` with ``fromAsset=USDT``,
        ``targetAsset=BFUSD``. The minted BFUSD lands in the unified
        margin pool where it (a) earns daily yield and (b) counts as
        cross-collateral for futures positions. There is no Binance UI
        "auto-toggle" for USDT→BFUSD conversion (a common
        misconception) — every conversion is an explicit mint call,
        which the bot now issues automatically as part of the earn
        sweep flow."""
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        # Cooldown reuses the existing per-asset throttle so we don't
        # spam Binance with mint calls if a previous one is still
        # propagating.
        cool_until = self._earn_subscribe_cooldown_until.get('BFUSD', 0.0)
        now = time.time()
        if now < cool_until:
            remaining = int(cool_until - now)
            return False, f'BFUSD: mint cooldown active ({remaining}s remaining)'
        fn = getattr(self.spot, 'sapiPostPortfolioMint', None) or getattr(self.spot, 'sapi_post_portfolio_mint', None)
        if fn is None:
            return False, 'BFUSD mint endpoint (sapiPostPortfolioMint) not exposed by this ccxt build'
        try:
            resp = fn({
                'fromAsset': 'USDT',
                'targetAsset': 'BFUSD',
                'amount': f'{amount_usdt:.2f}',
            })
        except Exception as e:
            err = str(e)
            if BINANCE_ERR_EARN_TOO_MANY_SUBS in err:
                self._earn_subscribe_cooldown_until['BFUSD'] = now + self.EARN_SUBSCRIBE_RATE_LIMITED_COOLDOWN_S
                return False, f'BFUSD mint rate-limited (77505), backing off 24h: {err[:120]}'
            return False, f'BFUSD mint failed: {err[:160]}'
        self._earn_subscribe_cooldown_until['BFUSD'] = now + self.EARN_SUBSCRIBE_DEFAULT_COOLDOWN_S
        return True, ''

    def earn_redeem(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        """Redeem BFUSD back to USDT via ``/sapi/v1/portfolio/redeem``.
        Triggered pre-trade by ``provision_margin`` when the bot needs
        more raw USDT than the unified pool currently has."""
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        fn = getattr(self.spot, 'sapiPostPortfolioRedeem', None) or getattr(self.spot, 'sapi_post_portfolio_redeem', None)
        if fn is None:
            return False, 'BFUSD redeem endpoint (sapiPostPortfolioRedeem) not exposed by this ccxt build'
        try:
            fn({
                'fromAsset': 'BFUSD',
                'targetAsset': 'USDT',
                'amount': f'{amount_usdt:.2f}',
            })
        except Exception as e:
            return False, f'BFUSD redeem failed: {str(e)[:160]}'
        return True, ''

    def transfer_spot_to_futures(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        # PM unifies margin; there's nothing to transfer.
        return True, 'PM mode: unified margin (no spot↔futures transfer needed)'

    def transfer_futures_to_spot(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        return True, 'PM mode: unified margin (no spot↔futures transfer needed)'

    def open_perp_positions_raw(self) -> list[dict]:
        """Replace the Classic ``futures.fetch_positions()`` (which hits
        /fapi/v2/positionRisk and returns -2015 under PM) with the PM
        equivalent ``/papi/v1/um/positionRisk``."""
        fn = self._papi(self.spot, ('papiGetUmPositionRisk', 'papi_get_um_position_risk'))
        if fn is None:
            return []
        try:
            rows = fn({})
        except Exception:
            return []
        # Convert PM rows to ccxt-shaped position dicts that downstream
        # leg-state checks expect (symbol, contracts, etc.).
        out: list[dict] = []
        for r in rows or []:
            try:
                amt = float(r.get('positionAmt') or 0)
            except (TypeError, ValueError):
                continue
            if abs(amt) <= 0:
                continue
            sym_raw = r.get('symbol') or ''  # e.g. "ETHUSDT"
            # Convert to ccxt-style "ETH/USDT:USDT"
            base = sym_raw[:-4] if sym_raw.endswith('USDT') else sym_raw
            ccxt_symbol = f'{base}/USDT:USDT' if base else sym_raw
            out.append({
                'symbol': ccxt_symbol,
                'contracts': abs(amt),
                'side': 'long' if amt > 0 else 'short',
                'info': r,
            })
        return out

    def configure_perp_for_arb(self, symbol: str) -> tuple[bool, str]:
        """Override of the base method. Under PM, margin mode is implicit
        cross — only leverage is configurable. Reads ``max_perp_leverage``
        from the StrategyConfig at call time so dashboard edits take effect
        without a restart. Calls /papi/v1/um/leverage."""
        # Strip ccxt's "ETH/USDT:USDT" suffix to Binance's "ETHUSDT".
        ex_symbol = symbol.split(':')[0].replace('/', '') if '/' in symbol else symbol
        fn = self._papi(self.spot, ('papiPostUmLeverage', 'papi_post_um_leverage'))
        if fn is None:
            return False, 'papiPostUmLeverage not exposed by this ccxt build'
        # Lazy import to avoid circular import with app.bot.
        try:
            from app.bot import get_strategy_config
            from app.db import SessionLocal as _SL
            with _SL() as db:
                leverage = max(1, int(get_strategy_config(db).max_perp_leverage or 1))
        except Exception:
            leverage = 1
        try:
            fn({'symbol': ex_symbol, 'leverage': leverage})
        except Exception as e:
            msg = str(e)
            if 'no need to change' not in msg.lower() and 'already' not in msg.lower():
                return False, msg
        return True, ''

    def _market_order(self, leg: str, symbol: str, side: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float) -> dict:
        """PM-routed market order. Spot legs go through margin-spot
        (``/papi/v1/margin/order``) since PM doesn't expose Classic spot
        for trading; futures legs go through UM (``/papi/v1/um/order``).
        Paper mode uses the parent class's synthesized fill."""
        if paper_mode:
            return super()._market_order(leg, symbol, side, amount, paper_mode, slippage_bps, fee_bps)
        ex_symbol = symbol.split(':')[0].replace('/', '') if '/' in symbol else symbol
        if leg == 'futures':
            fn = self._papi(self.spot, ('papiPostUmOrder', 'papi_post_um_order'))
            if fn is None:
                raise RuntimeError('papiPostUmOrder not exposed by this ccxt build')
            params = {
                'symbol': ex_symbol,
                'side': side.upper(),
                'type': 'MARKET',
                'quantity': self.futures.amount_to_precision(symbol, amount),
            }
        else:
            fn = self._papi(self.spot, ('papiPostMarginOrder', 'papi_post_margin_order'))
            if fn is None:
                raise RuntimeError('papiPostMarginOrder not exposed by this ccxt build')
            params = {
                'symbol': ex_symbol,
                'side': side.upper(),
                'type': 'MARKET',
                'quantity': self.spot.amount_to_precision(symbol, amount),
            }
        resp = fn(params)
        # Normalise the response into ccxt-shaped fill dict so downstream
        # record_trade / position-update logic stays unchanged.
        try:
            fill_price = float(resp.get('avgPrice') or resp.get('price') or 0)
        except (TypeError, ValueError):
            fill_price = 0.0
        try:
            executed_qty = float(resp.get('executedQty') or resp.get('cumQty') or amount)
        except (TypeError, ValueError):
            executed_qty = amount
        # Sum commission across the fills array if present, else 0.
        fee_cost = 0.0
        for f in resp.get('fills') or []:
            try:
                fee_cost += float(f.get('commission') or 0)
            except (TypeError, ValueError):
                pass
        # invalidate_balance_cache dropped — 30s TTL is short enough; eager invalidation triggered KuCoin 429s
        return {
            'id': str(resp.get('orderId') or ''),
            'symbol': symbol,
            'side': side,
            'amount': executed_qty,
            'venue': leg,
            'status': 'closed',
            'price': fill_price,
            'fee': {'cost': fee_cost},
            'info': resp,
        }


# ─── KuCoin gateway ─────────────────────────────────────────────────────────
# Implements the venue-specific overrides; everything else inherits from
# :class:`VenueGateway`. Notable differences vs Binance:
#   * No Portfolio-Margin equivalent. KuCoin offers Cross-Margin Spot and a
#     Unified Trading Account (UTA) but neither cross-collateralises spot,
#     futures, and earn the way Binance PM does for our funding-arb shape.
#     UTA does pool spot+futures collateral but its API surface diverges
#     significantly (different order endpoints, different liquidation
#     model) and KuCoin's yield-bearing collateral assets are limited. We
#     therefore stay on KuCoin's classic isolated wallets:
#       trade   = spot trading wallet (Trading Account in UI)
#       contract = USDM-perp futures wallet
#       main     = funding wallet — what KuCoin's auto-lend draws from,
#                  so the bot models it as the "Earn" surface.
#     Future cross-venue trade types (binance ↔ kucoin) are orchestrated
#     externally and don't need PM-style cross collateral on KuCoin.
#   * Spot ↔ futures transfers use ccxt's unified ``transfer()`` so the
#     v3 universal-transfer endpoint is invoked for trade ↔ contract;
#     inner-transfer is only used for spot-side wallet moves.
#   * Capital-flow history reads ``privateGetTransferList`` (v1 audit log)
#     plus deposit/withdrawal history. fetch_transfers fills any gaps.
#   * Symbol shape: ccxt normalises both KuCoin's perp suffix
#     (``XBTUSDTM`` → ``BTC/USDT:USDT``) and Binance's, so the bot's symbol
#     handling is unchanged across venues.

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
        # UTA detection — cached at init so per-method overrides don't
        # round-trip the network on every call. The probe is cheap (single
        # /api/v3/uta/check call); failures fall back to Classic to stay
        # safe. The bot loop and dashboard refresh the gateway frequently
        # enough that a UTA flip in the KuCoin UI is picked up quickly.
        self._is_uta = False
        try:
            self._is_uta = bool(self.spot.is_uta_enabled())
        except Exception:
            self._is_uta = False

    # ─── UTA helpers ────────────────────────────────────────────────────
    # ccxt names the UTA private routes ``utaPrivate{Get,Post}…``. We
    # probe both the camel- and snake-case variants because ccxt has
    # historically renamed these in minor releases.

    @staticmethod
    def _uta(client, candidates: tuple[str, ...]):
        for name in candidates:
            fn = getattr(client, name, None)
            if callable(fn):
                return fn
        return None

    def _ex_symbol_uta(self, ccxt_symbol: str) -> str:
        """KuCoin UTA expects raw exchange symbols (e.g. ``ETHUSDTM`` for
        the perp), not ccxt-normalised ``ETH/USDT:USDT``. Use ccxt's
        ``market`` lookup which carries the venue's id field."""
        try:
            m = self.futures.markets.get(ccxt_symbol) or self.spot.markets.get(ccxt_symbol)
            if m and m.get('id'):
                return str(m['id'])
        except Exception:
            pass
        return ccxt_symbol.split(':')[0].replace('/', '')

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
        # Two paths depending on account mode:
        #   UTA — single ``/api/v3/uta/account/balance`` call returns a
        #         unified pool of every collateral asset. We synthesise
        #         spot/earn/futures buckets so downstream code (which
        #         was written against Classic shape) keeps working: the
        #         unified-pool USDT lands in 'spot', 0 in 'futures' (UTA
        #         doesn't separate), auto-lent USDT in 'earn'.
        #   Classic — three separate fetch_balance calls (trade / main /
        #         contract); same as before.
        if self._is_uta:
            fn = self._uta(self.spot, ('utaPrivateGetAccountBalance', 'utaprivateGetAccountBalance'))
            if fn is not None:
                try:
                    resp = fn({})
                    rows = (resp or {}).get('data')
                    if isinstance(rows, dict):
                        rows = rows.get('list') or rows.get('balances') or []
                    elif not isinstance(rows, list):
                        rows = []
                    usdt_total = usdt_free = lent_total = 0.0
                    per_asset: dict[str, dict] = {}
                    for r in rows or []:
                        asset = r.get('coin') or r.get('asset') or r.get('currency') or ''
                        try:
                            free = float(r.get('availableBalance') or r.get('free') or 0)
                            total = float(r.get('walletBalance') or r.get('total') or free)
                            lent = float(r.get('autoLendQuantity') or r.get('lentBalance') or 0)
                        except (TypeError, ValueError):
                            continue
                        if asset == 'USDT':
                            usdt_total += total
                            usdt_free += free
                            lent_total += lent
                        elif total > 0:
                            per_asset[asset] = {'free': free, 'used': max(0.0, total - free), 'total': total}
                    spot = {'USDT': {'free': usdt_free, 'used': max(0.0, usdt_total - usdt_free), 'total': usdt_total},
                            **per_asset,
                            'free': {'USDT': usdt_free, **{k: v['free'] for k, v in per_asset.items()}},
                            'used': {'USDT': max(0.0, usdt_total - usdt_free), **{k: v['used'] for k, v in per_asset.items()}},
                            'total': {'USDT': usdt_total, **{k: v['total'] for k, v in per_asset.items()}}}
                    # UTA unifies spot + futures into one pool; mirror the
                    # PM convention — fut.free reflects pool capacity for
                    # the bot's trade-sizing min(), but fut.total is 0 so
                    # the equity-sum loop doesn't double-count the pool.
                    futures = {'USDT': {'free': usdt_free, 'used': 0.0, 'total': 0.0},
                               'free': {'USDT': usdt_free}, 'used': {'USDT': 0.0},
                               'total': {'USDT': 0.0}}
                    earn = {'USDT': {'free': lent_total, 'used': 0.0, 'total': lent_total},
                            'free': {'USDT': lent_total}, 'used': {'USDT': 0.0}, 'total': {'USDT': lent_total}}
                    return {'spot': spot, 'earn': earn, 'futures': futures}
                except Exception:
                    # UTA balance probe failed — fall through to classic
                    # path. last_balance_error gets populated by the parent.
                    pass
        # Classic fall-through.
        return {
            'spot': self.spot.fetch_balance({'type': 'trade'}),
            'earn': self.spot.fetch_balance({'type': 'main'}),
            'futures': self.futures.fetch_balance(),
        }

    def account_type(self) -> tuple[str, str]:
        """Read KuCoin's account mode live. Calls ccxt's ``is_uta_enabled``
        (which queries /api/v3/uta/check) first; if UTA is on, also pulls
        the account-mode detail via ``utaPrivateGetAccountMode``. Returns
        ``(label, detail)`` for /monitoring so the operator can see what
        the API actually reports rather than a hardcoded string."""
        try:
            uta = bool(self.spot.is_uta_enabled())
        except Exception as e:
            return 'Unknown', f'UTA probe failed: {str(e)[:80]}'
        if uta:
            mode_detail = 'UTA enabled'
            fn = getattr(self.spot, 'utaPrivateGetAccountMode', None) or getattr(self.spot, 'utaprivateGetAccountMode', None)
            if callable(fn):
                try:
                    resp = fn({})
                    if isinstance(resp, dict):
                        d = resp.get('data') or {}
                        mode_str = d.get('mode') or d.get('accountMode') or ''
                        if mode_str:
                            mode_detail = f'UTA · {mode_str}'
                except Exception:
                    pass
            return 'Unified Trading Account (UTA)', mode_detail
        return 'Classic', 'isolated trade / contract / main wallets'

    def equity_buckets(self) -> list[dict]:
        """KuCoin-correct equity buckets. Two shapes depending on
        account mode:

        UTA — single unified-margin pool plus optional auto-lent USDT
        (yield-bearing). Buckets:
          * ``KuCoin · UTA USDT``           — unified-pool USDT
          * ``KuCoin · UTA auto-lent USDT`` — auto-lent portion (yield)
          * ``KuCoin · UTA collateral``     — non-USDT assets in the pool

        Classic — three isolated wallets (Trade, Contract, Main):
          * ``KuCoin · Trade USDT``         — spot trading wallet
          * ``KuCoin · Contract USDT``      — futures wallet
          * ``KuCoin · Main USDT (auto-lend)`` — funding wallet (the
            yield surface; auto-lend draws from here when toggled in
            the KuCoin UI)
          * ``KuCoin · Trade collateral``   — non-USDT spot assets

        Buckets with zero balance are omitted so the donut/legend
        stays tight."""
        bals = self.safe_balances() or {}
        items: list[dict] = []
        spot_usdt = float((bals.get('spot', {}).get('USDT') or {}).get('total') or 0)
        fut_usdt = float((bals.get('futures', {}).get('USDT') or {}).get('total') or 0)
        earn_usdt = float((bals.get('earn', {}).get('USDT') or {}).get('total') or 0)
        if self._is_uta:
            if spot_usdt > 0:
                items.append({'label': f'{self.name} · UTA USDT', 'value': spot_usdt, 'venue': self.venue_id, 'color': '#38bdf8'})
            if earn_usdt > 0:
                items.append({'label': f'{self.name} · UTA auto-lent USDT', 'value': earn_usdt, 'venue': self.venue_id, 'color': '#4ade80'})
        else:
            if spot_usdt > 0:
                items.append({'label': f'{self.name} · Trade USDT', 'value': spot_usdt, 'venue': self.venue_id, 'color': '#38bdf8'})
            if fut_usdt > 0:
                items.append({'label': f'{self.name} · Contract USDT', 'value': fut_usdt, 'venue': self.venue_id, 'color': '#fbbf24'})
            if earn_usdt > 0:
                items.append({'label': f'{self.name} · Main USDT (auto-lend surface)', 'value': earn_usdt, 'venue': self.venue_id, 'color': '#4ade80'})
        # Non-USDT collateral assets (spot leg of an open arb, etc.).
        collateral_value = 0.0
        META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
        for asset, bal in (bals.get('spot') or {}).items():
            if asset in META_KEYS or asset == 'USDT' or not isinstance(bal, dict):
                continue
            qty = float(bal.get('total') or 0)
            if qty <= 0:
                continue
            px = self.safe_price(f'{asset}/USDT') or 0
            collateral_value += qty * px
        if collateral_value > 0:
            label = f'{self.name} · UTA collateral' if self._is_uta else f'{self.name} · Trade collateral'
            items.append({'label': label, 'value': collateral_value, 'venue': self.venue_id, 'color': '#818cf8'})
        return items

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

    # ─── Auto-lend toggle (KuCoin) ────────────────────────────────────────
    # Calls /api/v1/margin/toggle-auto-lend with sensible defaults:
    #   isEnable     = True (or False to disable)
    #   currency     = USDT
    #   retainSize   = 0    — lend everything; sub-account API keys can
    #                          revoke per-call as needed
    #   dailyIntRate = 0    — accept any rate; better than missing fills
    #                          on tight days and the realised yield will
    #                          track the venue's clearing rate
    #   term         = 7    — KuCoin's standard lend term in days
    # Idempotent: repeated calls with the same isEnable just return the
    # existing state. The bot calls this once per startup so the setting
    # converges to whatever ``cfg.kucoin_auto_lend_enabled`` is.
    def toggle_auto_lend(self, enabled: bool = True, asset: str = 'USDT') -> tuple[bool, str]:
        fn = (getattr(self.spot, 'privatePostMarginToggleAutoLend', None)
              or getattr(self.spot, 'private_post_margin_toggle_auto_lend', None))
        if fn is None:
            return False, 'privatePostMarginToggleAutoLend not exposed by this ccxt build'
        try:
            fn({
                'currency': asset,
                'isEnable': bool(enabled),
                'retainSize': '0',
                'dailyIntRate': '0',
                'term': 7,
            })
        except Exception as e:
            return False, str(e)[:200]
        return True, ''

    def earn_subscribe(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        if self._is_uta:
            # Under UTA, idle USDT auto-lends if the user enables auto-lend
            # in the KuCoin UI. The bot doesn't issue explicit subscribe
            # calls — that races with KuCoin's auto-routing.
            return True, 'UTA mode: auto-lend handles USDT yield (toggle in KuCoin UI per asset)'
        return self._transfer('trade', 'main', amount_usdt)

    def earn_redeem(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount_usdt <= 0:
            return True, 'noop'
        if self._is_uta:
            return True, 'UTA mode: auto-lend redeems automatically when margin is needed'
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
        # Under UTA the unified pool handles everything — no transfers
        # between trade / contract / main needed.
        if self._is_uta:
            return True, 'UTA mode: unified pool, no transfer needed'
        try:
            self.spot.transfer('USDT', float(amount_usdt), from_account, to_account)
        except Exception as e:
            return False, str(e)
        # invalidate_balance_cache dropped — 30s TTL is short enough; eager invalidation triggered KuCoin 429s
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
    def net_injected_capital_usdt(self, lookback_days: int = 10) -> tuple[float | None, dict]:
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

    def list_capital_flow_records(self, lookback_days: int = 10) -> list[dict]:
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
        # KuCoin's bespoke sub-transfer endpoints (privateGetTransferList,
        # privateGetSubTransferRecord, etc.) are either missing or 404 in
        # current ccxt builds. We rely on ccxt's unified fetch_transfers
        # below, which queries the v3 universal-transfer log and surfaces
        # master ↔ sub moves to a sub-account API key.
        # ccxt's unified fetch_transfers — KuCoin's universal-transfer log
        # surfaces master ↔ sub moves and inner-account moves. We filter
        # intra-account ones below so Net Injected Capital is clean.
        try:
            for t in (self.spot.fetch_transfers('USDT', since=since_ms) or []):
                ts = _ms_to_dt(t.get('timestamp'))
                raw_amt = float(t.get('amount') or 0)
                from_a = (t.get('fromAccount') or '').lower()
                to_a = (t.get('toAccount') or '').lower()
                # Filter intra-account transfers — these are bot-driven
                # moves between trade / main / contract on the same KuCoin
                # account, not capital flowing in/out.
                INTRA_KC = {'main', 'trade', 'contract', 'margin', 'isolated', 'pool', 'mining', 'unified'}
                if from_a in INTRA_KC and to_a in INTRA_KC:
                    continue
                # KuCoin's master-sub flag rides on transferType / type
                # rather than from/to labels — fetch the type field for the
                # direction inference. 'IN' is master→this-sub, 'OUT' is
                # this-sub→master.
                direction = (t.get('type') or '').upper()
                if direction == 'IN':
                    signed = abs(raw_amt)
                elif direction == 'OUT':
                    signed = -abs(raw_amt)
                else:
                    # Fall back: anything with 'sub' on either side that
                    # isn't intra-account — assume inflow if toAccount is
                    # the current account (no 'sub' label), outflow if
                    # toAccount is a sub.
                    signed = -abs(raw_amt) if 'sub' in to_a else abs(raw_amt)
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
