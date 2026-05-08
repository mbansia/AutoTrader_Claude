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
* :class:`BinanceGateway` — Binance spot + USDM-perp + universal-transfer
  SAPI + deposit/withdrawal/sub-transfer history.
* :class:`KuCoinGateway` — KuCoin spot + futures + ``innerTransfer``
  SAPI. Capital-flow history is deferred; ``net_injected_capital_usdt``
  returns ``None`` so the dashboard falls back to manual capital flows.

Design invariants
-----------------
* Every method that touches the network is wrapped to fail-soft: it
  returns ``None`` / ``[]`` / ``(False, err)`` rather than letting an
  exception escape into the cycle loop. A transient outage on one
  venue must not break the cycle for the others.
* SAPI methods are looked up through a small list of ccxt naming
  conventions (``sapiV1Get…`` / ``sapi_v1_get_…``) so the gateway
  works across the ccxt-version drift between dev and production.
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


# ─── Quote currencies the bot trades ─────────────────────────────────────
# Each is a separate book: USDT trades use USDT balance, USDC trades use
# USDC balance. The bot never cross-funds (a USDT shortfall is NOT
# covered by USDC and vice versa). For headline equity we price USDC at
# the live USDC/USDT mid, but the per-quote balances are tracked
# independently end-to-end.
SUPPORTED_QUOTES = ('USDT', 'USDC')


# ─── Candidate dataclass ────────────────────────────────────────────────────
# A scan result row: one funding-rate opportunity that passed every filter
# (entry threshold, volume, liquidity, etc.). The bot ranks candidates by
# ``funding_apr`` (with depth as the tiebreak) to decide which to open next.

@dataclass
class Candidate:
    spot_symbol: str
    perp_symbol: str
    funding_rate: float
    funding_interval_hours: float
    quote_volume: float
    spot_depth_usdt: float = 0.0
    perp_depth_usdt: float = 0.0
    venue_id: str = 'binance'
    quote_currency: str = 'USDT'       # PERP quote currency. Drives perp-leg
                                       # margin and futures wallet routing.
    spot_quote_currency: str = 'USDT'  # SPOT quote currency. Often equal to
                                       # quote_currency, but for a cross-
                                       # stable arb (e.g. DOGEUSDC perp +
                                       # DOGE/USDT spot) the two diverge —
                                       # the spot leg consumes this quote's
                                       # free balance, the perp leg consumes
                                       # ``quote_currency``. The 1:1ish USDC
                                       # ↔ USDT peg makes the resulting
                                       # delta-neutral hedge real, with the
                                       # tiny per-cycle depeg risk paid for
                                       # many times over by typical
                                       # cross-stable funding spreads.

    @property
    def funding_apr(self) -> float:
        return annualize_rate(self.funding_rate, self.funding_interval_hours)

    @property
    def funding_apy(self) -> float:
        return self.funding_apr

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
#   * implement the venue-specific methods at the bottom (transfers,
#     history). Default implementations are safe no-ops.

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
        # Per-quote coverage from the last scan_funding call. Populated
        # so the bot loop can surface "scanned N USDT + M USDC perps"
        # in scan_results.note for dashboard visibility.
        self.last_scan_per_quote: dict = {}
        # Markets cache freshness — load_markets() runs once at init,
        # but venues list new pairs continually (e.g. KuCoin added
        # DOGE/USDC mid-session, our cached market list missed it
        # until restart). Periodically reload so freshly-listed pairs
        # become tradable without operator intervention.
        self._markets_loaded_at: float = 0.0
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

    # Markets cache TTL — newly-listed pairs become tradable within this
    # window without needing a worker restart. KuCoin/Binance both list a
    # handful of new perps per day; an hour is short enough to catch them
    # before the funding rate decays, long enough to cost almost no rate
    # limit (each reload is one /api/v1/exchangeInfo call per client).
    MARKETS_RELOAD_TTL_S = 3600.0

    def load_markets(self, *, force: bool = False) -> None:
        """Reload ccxt's cached spot + futures market lists. Called
        once at startup and then opportunistically (with the TTL above
        as a throttle) by ``maybe_reload_markets`` so freshly-listed
        pairs become tradable without restarting the worker."""
        self.spot.load_markets(reload=force)
        self.futures.load_markets(reload=force)
        self._markets_loaded_at = time.time()

    def maybe_reload_markets(self, *, force: bool = False) -> bool:
        """Reload markets if older than ``MARKETS_RELOAD_TTL_S`` (or if
        ``force=True``). Returns True iff a reload happened. Failures
        are swallowed — stale markets are better than a crashed cycle."""
        if not force and self._markets_loaded_at and (time.time() - self._markets_loaded_at) < self.MARKETS_RELOAD_TTL_S:
            return False
        try:
            self.load_markets(force=True)
            return True
        except Exception:
            return False

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
            self.last_balance_error = str(e)
            # Always preserve the last-good cached payload across any
            # API failure (rate-limit, network timeout, 5xx, parse
            # error). Showing a stale balance + a banner with the
            # error is far better UX than blanking the dashboard to
            # zero — the user explicitly asked for this in feedback
            # round 3. The cache only gets overwritten on a fresh
            # successful read above; never with None here.
            if self._balance_cache is not None and self._balance_cache[1] is not None:
                return self._balance_cache[1]
            return None

    def _fetch_balances_uncached(self) -> dict:
        """Override hook — subclasses with custom wallet topology (e.g.
        KuCoin's ``trade`` vs ``main`` split) replace this. Default is the
        plain ccxt fetch_balance() pair."""
        return {'spot': self.spot.fetch_balance(), 'futures': self.futures.fetch_balance()}

    def invalidate_balance_cache(self) -> None:
        """Drop the in-memory balance cache so the next ``safe_balances``
        call hits the API. Used right after orders / transfers so
        downstream code sees the post-action state."""
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
    ) -> tuple[list[Candidate], int, list[tuple[str, str, float]]]:
        """Scan this venue's USDT- AND USDC-perp funding rates. Returns
        ``(passing, total_examined, rejected)`` where:
        * ``passing`` is the ranked list of :class:`Candidate` rows that survived
          every filter (APR threshold, quote volume, liquidity).
        * ``total_examined`` is the number of perps whose funding rate ccxt
          returned — useful to confirm the API is actually live.
        * ``rejected`` is a list of ``(symbol, reason, apr)`` for the Logs tab.

        Each :class:`Candidate` carries ``venue_id`` and ``quote_currency``
        so the bot can route the entry to the matching wallet's free
        balance and never cross-fund USDT ↔ USDC."""
        # Refresh the markets cache opportunistically before scanning so
        # newly-listed pairs (DOGE/USDC was the canonical case) become
        # eligible without a worker restart. Throttled by the TTL —
        # each reload is one /exchangeInfo call per ccxt client.
        self.maybe_reload_markets()
        try:
            rates = self.funding_rates_dict()
        except Exception:
            return [], 0, []
        # First pass: filter by quote, threshold, and spot-market existence.
        # Per-symbol fetch_ticker calls used to be made here in a loop —
        # that took N round-trips and routinely tripped venue rate limits
        # (the user saw "ticker_error" rejections as a result). We now
        # do ONE batch fetch_tickers() for every spot symbol that
        # survived the cheap filters; one HTTP call instead of dozens.
        prefiltered: list[tuple[str, str, float, float, str]] = []
        passing: list[Candidate] = []
        rejected: list[tuple[str, str, float]] = []
        total = 0
        # Per-scan flag: do the force-reload at most once even if many
        # candidates are missing spot, so we don't spam /exchangeInfo.
        forced_reload_this_scan = False
        # Per-quote scan-coverage counts so the dashboard can surface
        # "scanned N USDT + M USDC perps" — gives the operator real
        # confidence that USDC pairs are reaching the scan, not silently
        # being filtered out somewhere upstream. Stored on the gateway
        # so the bot loop can read it without changing this method's
        # return signature.
        per_quote_total = {q: 0 for q in SUPPORTED_QUOTES}
        per_quote_below_threshold = {q: 0 for q in SUPPORTED_QUOTES}
        # Top below-threshold per quote so the dashboard's
        # rejected_candidates table can surface "DOGE/USDC missed entry
        # at 4.2% APY (threshold 10%)" without flooding the table with
        # every below-threshold row every cycle. We keep the top-10 by
        # APR per quote (closest-to-passing first).
        per_quote_top_below: dict[str, list[tuple[str, float]]] = {q: [] for q in SUPPORTED_QUOTES}
        for symbol, row in rates.items():
            fr = row.get('fundingRate')
            if fr is None:
                continue
            # Only USDT and USDC quote perps; ignore coin-margined and exotics.
            quote = None
            for q in SUPPORTED_QUOTES:
                if symbol.endswith(f':{q}'):
                    quote = q
                    break
            if quote is None:
                continue
            total += 1
            per_quote_total[quote] += 1
            interval_h = _interval_hours(row)
            apr = annualize_rate(float(fr), interval_h)
            if apr < entry_apr_threshold:
                per_quote_below_threshold[quote] += 1
                per_quote_top_below[quote].append((symbol, apr))
                continue
            base = symbol.split('/')[0]
            # Spot-leg pairing: spot is just spot — the stablecoin
            # we pay in doesn't change the long-base hedge. So we
            # accept ANY {base}/<supported_stable> spot pair, with a
            # mild preference for the perp's quote first (saves a
            # USDC↔USDT swap when both wallets aren't pre-funded).
            # If no spot pair exists for any supported quote, force-
            # reload markets ONCE and retry — handles pairs listed
            # mid-session that the cached market dict missed. Final
            # rejection enumerates every symbol checked so the
            # operator can see the bot really did try.
            tried = []
            spot_symbol = None
            spot_quote = quote
            for q_pref in (quote,) + tuple(q for q in SUPPORTED_QUOTES if q != quote):
                cand = f'{base}/{q_pref}'
                tried.append(cand)
                if cand in self.spot.markets:
                    spot_symbol = cand
                    spot_quote = q_pref
                    break
            if spot_symbol is None and not forced_reload_this_scan:
                # First miss in this scan — force-reload markets once
                # (cap is per-scan, not per-candidate, so we never
                # spam /exchangeInfo). A single reload repopulates
                # `self.spot.markets` for every pair, so subsequent
                # missing-spot lookups re-check the fresh dict for
                # free without another network call.
                forced_reload_this_scan = True
                self.maybe_reload_markets(force=True)
                for q_pref in (quote,) + tuple(q for q in SUPPORTED_QUOTES if q != quote):
                    cand = f'{base}/{q_pref}'
                    if cand in self.spot.markets:
                        spot_symbol = cand
                        spot_quote = q_pref
                        break
            if spot_symbol is None:
                rejected.append((symbol, f'no_spot_market (tried {", ".join(tried)})', apr))
                continue
            prefiltered.append((symbol, spot_symbol, float(fr), interval_h, quote, spot_quote))

        # Batch ticker fetch for everything that passed the cheap filters.
        # ccxt's spot.fetch_tickers(symbols) returns {symbol: ticker} in
        # one HTTP call on both Binance and KuCoin, so we never hit the
        # per-symbol rate limit that produced "ticker_error" rejections.
        spot_symbols_needed = sorted({s[1] for s in prefiltered})
        tickers: dict = {}
        if spot_symbols_needed:
            try:
                tickers = self.spot.fetch_tickers(spot_symbols_needed) or {}
            except Exception as e:
                # Fall back to fetching all tickers (no symbol arg) — some
                # ccxt versions don't accept a symbol list on fetch_tickers
                # for KuCoin. If that also fails the candidates without a
                # ticker get rejected with the actual error message.
                try:
                    tickers = self.spot.fetch_tickers() or {}
                except Exception as e2:
                    e = e2
                    msg = str(e)[:60]
                    for sym, _, _, _, _, _ in prefiltered:
                        rejected.append((sym, f'ticker_fetch_failed: {msg}', annualize_rate(0.0, 8.0)))
                    prefiltered = []

        for symbol, spot_symbol, fr_v, interval_h, quote, spot_quote in prefiltered:
            apr = annualize_rate(fr_v, interval_h)
            t = tickers.get(spot_symbol) or {}
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
            passing.append(Candidate(
                spot_symbol=spot_symbol,
                perp_symbol=symbol,
                funding_rate=fr_v,
                funding_interval_hours=interval_h,
                quote_volume=qv,
                spot_depth_usdt=spot_depth,
                perp_depth_usdt=perp_depth,
                venue_id=self.venue_id,
                quote_currency=quote,
                spot_quote_currency=spot_quote,
            ))
        # Rank by funding APY first; ties go to the deeper book.
        passing.sort(key=lambda c: (c.funding_apr, c.min_depth_usdt), reverse=True)
        # Stash per-quote coverage for the scan-result note.
        per_quote_passing = {q: 0 for q in SUPPORTED_QUOTES}
        for c in passing:
            per_quote_passing[c.quote_currency] = per_quote_passing.get(c.quote_currency, 0) + 1
        self.last_scan_per_quote = {
            q: {
                'total': per_quote_total.get(q, 0),
                'below_threshold': per_quote_below_threshold.get(q, 0),
                'passing': per_quote_passing.get(q, 0),
            } for q in SUPPORTED_QUOTES
        }
        # Surface the top below-threshold candidates per quote into
        # the rejected list with a clear "below_threshold" reason. We
        # cap at TOP_BELOW_PER_QUOTE per quote per scan to keep the
        # rejected_candidates table tractable (≤20 extra rows per
        # cycle per venue), while still giving the operator visibility
        # into which symbols are closest to passing — including the
        # USDC ones whose funding rates rarely clear the threshold
        # (so they'd otherwise be silently invisible).
        TOP_BELOW_PER_QUOTE = 10
        thresh_pct = entry_apr_threshold * 100.0
        for q, rows in per_quote_top_below.items():
            rows.sort(key=lambda r: r[1], reverse=True)
            for sym, apr_val in rows[:TOP_BELOW_PER_QUOTE]:
                rejected.append((sym, f'below_threshold ({apr_val*100:.2f}% APY < {thresh_pct:.2f}%)', apr_val))
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

    def transfer_spot_to_futures(self, amount: float, paper_mode: bool, asset: str = 'USDT') -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, f'{asset} spot→futures transfer not wired on {self.name}'

    def transfer_futures_to_spot(self, amount: float, paper_mode: bool, asset: str = 'USDT') -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, f'{asset} futures→spot transfer not wired on {self.name}'

    def net_injected_capital_usdt(self, lookback_days: int = 30) -> tuple[float | None, dict]:
        """Returns ``(net, meta)`` where ``net`` is in USDT and ``meta`` carries
        a per-component breakdown for the UI. ``None`` signals the caller to
        fall back to manual CapitalFlow rows."""
        return None, {'error': f'history not wired on {self.name}'}

    def list_capital_flow_records(self, lookback_days: int = 30) -> list[dict]:
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
        """Return per-(venue, asset) equity buckets — one line per
        stablecoin (sum of spot + futures wallets) plus one for
        non-stablecoin collateral. USDT and USDC are tracked as
        independent books; the dashboard prices USDC at live USDC/USDT
        mid for headline equity but renders the per-asset split here."""
        bals = self.safe_balances() or {}
        items: list[dict] = []
        QUOTE_COLORS = {'USDT': '#38bdf8', 'USDC': '#22d3ee'}
        for q in SUPPORTED_QUOTES:
            spot_amt = float((bals.get('spot', {}).get(q) or {}).get('total') or 0)
            fut_amt = float((bals.get('futures', {}).get(q) or {}).get('total') or 0)
            total = spot_amt + fut_amt
            if total > 0:
                items.append({'label': f'{self.name} · {q}', 'value': total, 'asset': q, 'venue': self.venue_id, 'color': QUOTE_COLORS[q]})
        spot_assets_value = 0.0
        META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
        for asset, bal in (bals.get('spot') or {}).items():
            if asset in META_KEYS or asset in SUPPORTED_QUOTES or not isinstance(bal, dict):
                continue
            qty = float(bal.get('total') or 0)
            if qty <= 0:
                continue
            px = self.safe_price(f'{asset}/USDT') or 0
            spot_assets_value += qty * px
        if spot_assets_value > 0:
            items.append({'label': f'{self.name} · collateral assets', 'value': spot_assets_value, 'asset': 'USDT', 'venue': self.venue_id, 'color': '#818cf8'})
        return items


# ─── Binance gateway ────────────────────────────────────────────────────────
# Implements the surface — universal-transfer, deposit / withdrawal /
# sub-account-transfer history. Uses Binance's USDM-futures client
# (``ccxt.binanceusdm``) to keep symbol shape identical to the spot
# client (``BTC/USDT:USDT`` for the perp, ``BTC/USDT`` for the spot).

class BinanceGateway(VenueGateway):
    venue_id = 'binance'
    name = 'Binance'

    # Caches — instance-scoped so they survive across method calls but die
    # with the gateway. Each carries an explicit TTL because capital-flow
    # history is stable for many minutes.
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

    # ─── Binance universal transfer (spot ⇄ USDM-futures) ─────────────────

    def _universal_transfer(self, transfer_type: str, amount: float, asset: str = 'USDT') -> tuple[bool, str]:
        for name in ('sapiPostAssetTransfer', 'sapi_post_asset_transfer'):
            fn = getattr(self.spot, name, None)
            if callable(fn):
                try:
                    fn({'type': transfer_type, 'asset': asset, 'amount': f'{amount:.2f}'})
                except Exception as e:
                    return False, str(e)
                return True, ''
        return False, 'sapiPostAssetTransfer not available in this ccxt build'

    def transfer_spot_to_futures(self, amount: float, paper_mode: bool, asset: str = 'USDT') -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount <= 0:
            return True, 'noop'
        return self._universal_transfer('MAIN_UMFUTURE', amount, asset=asset)

    def transfer_futures_to_spot(self, amount: float, paper_mode: bool, asset: str = 'USDT') -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount <= 0:
            return True, 'noop'
        return self._universal_transfer('UMFUTURE_MAIN', amount, asset=asset)

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

    def deposit_history(self, asset: str = 'USDT', lookback_days: int = 30, ttl_seconds: float = 300.0) -> list[dict]:
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

    def withdrawal_history(self, asset: str = 'USDT', lookback_days: int = 30, ttl_seconds: float = 300.0) -> list[dict]:
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

    def sub_account_transfer_history(self, asset: str = 'USDT', incoming: bool = True, lookback_days: int = 30, ttl_seconds: float = 300.0) -> list[dict]:
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

    def net_injected_capital_usdt(self, lookback_days: int = 30) -> tuple[float | None, dict]:
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

    def list_capital_flow_records(self, lookback_days: int = 30) -> list[dict]:
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
    # Binance Portfolio Margin replaces the Classic spot/futures split with
    # a unified margin pool that holds USDT, USDC, and other margin assets
    # simultaneously. Orders route through ``/papi/v1/*`` (separate from
    # ``/api/v3/*`` and ``/fapi/v1/*``); calling Classic endpoints on a PM
    # account returns -2015. We always go through the PM paths — no
    # Classic fallback to maintain.
    #
    # Endpoint references (Binance API docs, Sept 2024 revision):
    #   /papi/v1/balance           — unified balance per asset
    #   /papi/v1/account           — overall account / margin state
    #   /papi/v1/um/order          — USDM-perp order placement
    #   /papi/v1/um/leverage       — set leverage on a UM symbol
    #   /papi/v1/margin/order      — cross-margin spot order placement
    #   /papi/v1/um/positionRisk   — open UM positions

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
        """Per-(venue, asset) equity buckets — one line per stablecoin
        + one for non-stablecoin collateral. USDT and USDC are tracked
        as separate books; the dashboard prices USDC at live USDC/USDT
        mid for headline equity but renders the per-asset split here."""
        bals = self.safe_balances() or {}
        items: list[dict] = []
        QUOTE_COLORS = {'USDT': '#38bdf8', 'USDC': '#22d3ee'}
        for q in SUPPORTED_QUOTES:
            amt = float((bals.get('spot', {}).get(q) or {}).get('total') or 0)
            if amt > 0:
                items.append({'label': f'{self.name} · {q}', 'value': amt, 'asset': q, 'venue': self.venue_id, 'color': QUOTE_COLORS[q]})
        collateral_value = 0.0
        META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
        for asset, bal in (bals.get('spot') or {}).items():
            if asset in META_KEYS or asset in SUPPORTED_QUOTES or not isinstance(bal, dict):
                continue
            qty = float(bal.get('total') or 0)
            if qty <= 0:
                continue
            px = self.safe_price(f'{asset}/USDT') or 0
            collateral_value += qty * px
        if collateral_value > 0:
            items.append({'label': f'{self.name} · collateral assets', 'value': collateral_value, 'asset': 'USDT', 'venue': self.venue_id, 'color': '#818cf8'})
        return items

    def _fetch_balances_uncached(self) -> dict:
        """Read PM unified balance + classic Spot wallet, then synthesise
        the bot's two-bucket shape (spot · futures). Both quote currencies
        the bot trades — USDT and USDC — are surfaced as top-level keys
        in spot/futures so per-quote sizing stays self-contained."""
        fn = self._papi(self.spot, ('papiGetBalance', 'papi_get_balance'))
        if fn is None:
            return {'spot': self.spot.fetch_balance(), 'futures': self.futures.fetch_balance()}
        rows = fn({})
        if isinstance(rows, dict):
            rows = rows.get('data') or rows.get('balances') or []
        # Track USDT and USDC separately end-to-end. The bot never
        # cross-funds: a USDC entry only consumes USDC free balance, etc.
        quote_totals = {q: 0.0 for q in SUPPORTED_QUOTES}
        quote_free = {q: 0.0 for q in SUPPORTED_QUOTES}
        per_asset: dict[str, dict] = {}
        for r in rows:
            asset = r.get('asset') or r.get('currency') or ''
            try:
                free = float(r.get('crossMarginFree') or r.get('umWalletBalance') or r.get('free') or 0)
                total = float(r.get('totalWalletBalance') or r.get('crossMarginAsset') or r.get('total') or free)
            except (TypeError, ValueError):
                continue
            if asset in SUPPORTED_QUOTES:
                quote_totals[asset] += total
                quote_free[asset] += free
            elif total > 0:
                per_asset[asset] = {'free': free, 'used': max(0.0, total - free), 'total': total}
        try:
            spot_classic = self.spot.fetch_balance()
            for q in SUPPORTED_QUOTES:
                bal = spot_classic.get(q) or {}
                quote_totals[q] += float(bal.get('total') or 0)
                quote_free[q] += float(bal.get('free') or 0)
            META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
            for asset, bal in spot_classic.items():
                if asset in META_KEYS or asset in SUPPORTED_QUOTES or not isinstance(bal, dict):
                    continue
                qty = float(bal.get('total') or 0)
                if qty <= 0:
                    continue
                if asset not in per_asset:
                    per_asset[asset] = {'free': float(bal.get('free') or 0), 'used': float(bal.get('used') or 0), 'total': qty}
        except Exception:
            pass
        spot: dict = {**per_asset}
        for q in SUPPORTED_QUOTES:
            tot = quote_totals[q]
            fr = quote_free[q]
            spot[q] = {'free': fr, 'used': max(0.0, tot - fr), 'total': tot}
        spot['free'] = {**{q: quote_free[q] for q in SUPPORTED_QUOTES}, **{k: v['free'] for k, v in per_asset.items()}}
        spot['used'] = {**{q: max(0.0, quote_totals[q] - quote_free[q]) for q in SUPPORTED_QUOTES}, **{k: v['used'] for k, v in per_asset.items()}}
        spot['total'] = {**{q: quote_totals[q] for q in SUPPORTED_QUOTES}, **{k: v['total'] for k, v in per_asset.items()}}
        # Under PM there is no separate futures wallet. Expose each
        # quote's free amount on the futures side so trade-sizing
        # min(spot_free, fut_free) still works per-quote, but `total` is
        # 0 so the equity sum doesn't double-count the unified pool.
        futures: dict = {}
        for q in SUPPORTED_QUOTES:
            futures[q] = {'free': quote_free[q], 'used': 0.0, 'total': 0.0}
        futures['free'] = {q: quote_free[q] for q in SUPPORTED_QUOTES}
        futures['used'] = {q: 0.0 for q in SUPPORTED_QUOTES}
        futures['total'] = {q: 0.0 for q in SUPPORTED_QUOTES}
        return {'spot': spot, 'futures': futures}

    def transfer_spot_to_futures(self, amount: float, paper_mode: bool, asset: str = 'USDT') -> tuple[bool, str]:
        # PM unifies margin; there's nothing to transfer.
        return True, 'PM mode: unified margin (no spot↔futures transfer needed)'

    def transfer_futures_to_spot(self, amount: float, paper_mode: bool, asset: str = 'USDT') -> tuple[bool, str]:
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
#   * Two account modes: Classic (isolated trade / contract / main wallets)
#     and Unified Trading Account (UTA, pooled cross-margin). The gateway
#     auto-detects via ``is_uta_enabled()`` and reads either path.
#   * Spot ↔ futures transfers use ccxt's unified ``transfer()`` so the
#     v3 universal-transfer endpoint is invoked for trade ↔ contract.
#   * Capital-flow history reads ``privateGetTransferList`` (v1 audit log)
#     plus deposit/withdrawal history. fetch_transfers fills any gaps.
#   * Symbol shape: ccxt normalises both KuCoin's perp suffix
#     (``XBTUSDTM`` → ``BTC/USDT:USDT``) and Binance's.

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
    # surface ``predictedFundingFeeRate`` + ``nextFundingRateTime`` on every
    # active contract via ``/api/v1/contracts/active`` (which is what
    # load_markets() already calls). We rebuild the ccxt-shaped funding-
    # rate dict from the cached market info — no extra HTTP per scan.
    #
    # IMPORTANT: ``predictedFundingFeeRate`` is the rate the upcoming
    # settlement WILL pay (what KuCoin's web UI shows as "Funding Rate
    # / Countdown" — and what an arb bot actually cares about), while
    # plain ``fundingFeeRate`` is the rate from the LAST settlement
    # (often near zero between cycles after the rate snaps back to
    # equilibrium). The earlier code used fundingFeeRate, which made
    # rejected_candidates report wildly understated APYs (e.g. 11.57%
    # for a perp the operator could see at 0.525% / 8h ≈ 575% APY in
    # the venue UI). Prefer predicted; fall back to last-applied if
    # the predicted field is missing or null.
    def funding_rates_dict(self) -> dict:
        if not self.futures.markets:
            self.futures.load_markets()
        out: dict = {}
        for symbol, market in self.futures.markets.items():
            if market.get('type') != 'swap' or market.get('quote') not in SUPPORTED_QUOTES:
                continue
            info = market.get('info') or {}
            fr = info.get('predictedFundingFeeRate')
            if fr is None:
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
    #   * ``main``     — Funding Account (UI label); deposits land here.
    #   * ``contract`` — Futures wallet; perp orders consume margin here.
    #
    # ccxt's default ``fetch_balance()`` only returns ``trade``. We expose
    # ``trade`` as ``bals['spot']`` and the contract balance as
    # ``bals['futures']``.
    def _fetch_balances_uncached(self) -> dict:
        # Two paths depending on account mode:
        #   UTA — single ``/api/v3/uta/account/balance`` call returns a
        #         unified pool of every collateral asset. We synthesise
        #         spot/futures buckets so downstream code keeps working:
        #         the unified-pool USDT lands in 'spot', 0 in 'futures'
        #         (UTA doesn't separate).
        #   Classic — separate fetch_balance calls (trade / contract).
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
                    quote_totals = {q: 0.0 for q in SUPPORTED_QUOTES}
                    quote_free = {q: 0.0 for q in SUPPORTED_QUOTES}
                    per_asset: dict[str, dict] = {}
                    for r in rows or []:
                        asset = r.get('coin') or r.get('asset') or r.get('currency') or ''
                        try:
                            free = float(r.get('availableBalance') or r.get('free') or 0)
                            total = float(r.get('walletBalance') or r.get('total') or free)
                        except (TypeError, ValueError):
                            continue
                        if asset in SUPPORTED_QUOTES:
                            quote_totals[asset] += total
                            quote_free[asset] += free
                        elif total > 0:
                            per_asset[asset] = {'free': free, 'used': max(0.0, total - free), 'total': total}
                    spot: dict = {**per_asset}
                    for q in SUPPORTED_QUOTES:
                        tot = quote_totals[q]
                        fr = quote_free[q]
                        spot[q] = {'free': fr, 'used': max(0.0, tot - fr), 'total': tot}
                    spot['free'] = {**{q: quote_free[q] for q in SUPPORTED_QUOTES}, **{k: v['free'] for k, v in per_asset.items()}}
                    spot['used'] = {**{q: max(0.0, quote_totals[q] - quote_free[q]) for q in SUPPORTED_QUOTES}, **{k: v['used'] for k, v in per_asset.items()}}
                    spot['total'] = {**{q: quote_totals[q] for q in SUPPORTED_QUOTES}, **{k: v['total'] for k, v in per_asset.items()}}
                    # UTA unifies spot + futures. fut.free mirrors the
                    # pool free for trade-sizing; fut.total is 0 to avoid
                    # double-counting the same pool in equity sums.
                    futures: dict = {}
                    for q in SUPPORTED_QUOTES:
                        futures[q] = {'free': quote_free[q], 'used': 0.0, 'total': 0.0}
                    futures['free'] = {q: quote_free[q] for q in SUPPORTED_QUOTES}
                    futures['used'] = {q: 0.0 for q in SUPPORTED_QUOTES}
                    futures['total'] = {q: 0.0 for q in SUPPORTED_QUOTES}
                    return {'spot': spot, 'futures': futures}
                except Exception:
                    pass
        # Classic — KuCoin splits cash across THREE wallet types
        # (``trade`` for spot orders, ``main`` for deposits/funding,
        # ``contract`` for perp margin). Reading just ``trade`` would
        # miss whatever the user has parked in the funding wallet —
        # which is the default landing spot for new deposits and
        # master→sub transfers. Sum trade + main into the synthesised
        # ``spot`` bucket so equity reflects the full account.
        trade_bal = self.spot.fetch_balance({'type': 'trade'})
        try:
            main_bal = self.spot.fetch_balance({'type': 'main'})
        except Exception:
            main_bal = {}
        spot: dict = {'free': {}, 'used': {}, 'total': {}}
        META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
        seen: set[str] = set()
        for source in (trade_bal, main_bal):
            for asset, bal in (source or {}).items():
                if asset in META_KEYS or not isinstance(bal, dict):
                    continue
                free_amt = float(bal.get('free') or 0)
                used_amt = float(bal.get('used') or 0)
                total_amt = float(bal.get('total') or 0)
                if total_amt <= 0:
                    continue
                if asset in seen:
                    spot[asset]['free'] += free_amt
                    spot[asset]['used'] += used_amt
                    spot[asset]['total'] += total_amt
                else:
                    spot[asset] = {'free': free_amt, 'used': used_amt, 'total': total_amt}
                    seen.add(asset)
                spot['free'][asset] = spot['free'].get(asset, 0.0) + free_amt
                spot['used'][asset] = spot['used'].get(asset, 0.0) + used_amt
                spot['total'][asset] = spot['total'].get(asset, 0.0) + total_amt
        return {
            'spot': spot,
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
        """Per-(venue, asset) equity buckets — one line per stablecoin
        (sum of trade + contract wallets) plus one for non-stablecoin
        collateral. USDT and USDC are tracked as independent books;
        the dashboard prices USDC at live USDC/USDT mid for headline
        equity but renders the per-asset split here."""
        bals = self.safe_balances() or {}
        items: list[dict] = []
        QUOTE_COLORS = {'USDT': '#38bdf8', 'USDC': '#22d3ee'}
        for q in SUPPORTED_QUOTES:
            spot_amt = float((bals.get('spot', {}).get(q) or {}).get('total') or 0)
            fut_amt = float((bals.get('futures', {}).get(q) or {}).get('total') or 0)
            total = spot_amt + fut_amt
            if total > 0:
                items.append({'label': f'{self.name} · {q}', 'value': total, 'asset': q, 'venue': self.venue_id, 'color': QUOTE_COLORS[q]})
        collateral_value = 0.0
        META_KEYS = {'info', 'free', 'used', 'total', 'timestamp', 'datetime'}
        for asset, bal in (bals.get('spot') or {}).items():
            if asset in META_KEYS or asset in SUPPORTED_QUOTES or not isinstance(bal, dict):
                continue
            qty = float(bal.get('total') or 0)
            if qty <= 0:
                continue
            px = self.safe_price(f'{asset}/USDT') or 0
            collateral_value += qty * px
        if collateral_value > 0:
            items.append({'label': f'{self.name} · collateral assets', 'value': collateral_value, 'asset': 'USDT', 'venue': self.venue_id, 'color': '#818cf8'})
        return items

    # ─── Spot ↔ futures transfer (KuCoin) ────────────────────────────────
    # KuCoin splits transfers across two endpoints:
    #   * /api/v1/accounts/inner-transfer — main ↔ trade ↔ margin (spot
    #     wallets only). Rejects ``contract`` with code 400100
    #     "from not in the given range".
    #   * /api/v3/accounts/universal-transfer — handles everything,
    #     including ``contract`` ↔ ``trade`` and master ↔ sub.
    # ccxt's unified ``transfer()`` picks the right one based on whether
    # the route involves a non-spot wallet, so we always go through it.
    def _transfer(self, from_account: str, to_account: str, amount: float, asset: str = 'USDT') -> tuple[bool, str]:
        # Under UTA the unified pool handles everything — no transfers
        # between trade / contract / main needed.
        if self._is_uta:
            return True, 'UTA mode: unified pool, no transfer needed'
        try:
            self.spot.transfer(asset, float(amount), from_account, to_account)
        except Exception as e:
            return False, str(e)
        return True, ''

    # KuCoin's spot orders execute against the ``trade`` wallet (UI label
    # "Trading Account"); ``contract`` is the futures wallet. Spot↔futures
    # transfers move trade↔contract under Classic; under UTA the unified
    # pool removes the need for these transfers entirely.
    def transfer_spot_to_futures(self, amount: float, paper_mode: bool, asset: str = 'USDT') -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount <= 0:
            return True, 'noop'
        return self._transfer('trade', 'contract', amount, asset=asset)

    def transfer_futures_to_spot(self, amount: float, paper_mode: bool, asset: str = 'USDT') -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        if amount <= 0:
            return True, 'noop'
        return self._transfer('contract', 'trade', amount, asset=asset)

    # ─── Capital-injection history (KuCoin) ───────────────────────────────
    # KuCoin sub-accounts: master→sub transfers come in via the
    # ``/api/v1/accounts/sub-transfer`` audit endpoint, and external
    # deposits via ``fetch_deposits``. ccxt exposes both. We sum USDT
    # inflows minus outflows over the lookback. Returns ``(None, meta)`` if
    # no endpoint returned anything, so the caller falls back to manual
    # CapitalFlow rows.
    def net_injected_capital_usdt(self, lookback_days: int = 30) -> tuple[float | None, dict]:
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

    def list_capital_flow_records(self, lookback_days: int = 30) -> list[dict]:
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
