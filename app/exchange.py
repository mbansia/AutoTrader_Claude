"""Venue gateways — single point of contact for all exchange operations.

The bot manages a single pool of capital distributed across venues. Each
venue (Binance today, KuCoin from Phase 1, Interactive Brokers later)
exposes the same surface so the bot loop can iterate uniformly:

* :class:`BinanceGateway` wraps ``ccxt.binance`` (spot) + ``ccxt.binanceusdm``
  (USDM-perp). Earn / transfer / deposit-history / sub-transfer are
  Binance-specific and live here.
* :class:`KuCoinGateway` wraps ``ccxt.kucoin`` (spot) + ``ccxt.kucoinfutures``
  (USDT-margined perps). Transfers use KuCoin's ``innerTransfer`` SAPI;
  Earn (Pool-X) is deferred to a later phase.
* :func:`make_gateways` is the factory the bot uses — it returns the
  ordered list of currently-configured venues based on env credentials.

Design notes
------------
* Every method that hits the network is wrapped to fail-soft (return
  ``None`` / empty / a ``(False, err)`` tuple) so a transient API issue
  doesn't crash the bot's main loop.
* SAPI methods are looked up across a couple of ccxt name conventions
  (``sapiV1Get…`` / ``sapi_v1_get_…``) so the gateway works across the
  ccxt-version drift the user might encounter on their deployment.
* Several caches (``_earn_product_id_cache``, ``_*_history_cache``) live
  on the instance with explicit TTLs to keep API calls cheap on routes
  that re-render frequently. They're invalidated by recreating the
  gateway (e.g. on restart) — that's intentional.
* Historical Binance error codes are tracked as named constants below
  so the bot's error-handling paths use semantically meaningful names
  rather than magic numbers.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import ccxt

from app.config import settings


# ─── Binance error codes we react to ────────────────────────────────────────
# These are returned in the ccxt exception message text when Binance rejects
# a request. We string-search rather than parse JSON because ccxt's message
# format isn't fully stable across versions.

BINANCE_ERR_INVALID_API = '-2015'           # bad key / IP not whitelisted / missing permission
BINANCE_ERR_INSUFFICIENT_MARGIN = '-2019'   # futures order can't be placed for lack of free margin
BINANCE_ERR_MALFORMED_AMOUNT = '-1102'      # amount param missing/empty/zero on a SAPI call
BINANCE_ERR_NO_EARN_POSITION = '-6053'      # trying to redeem from a flexible product with no balance
BINANCE_ERR_EARN_TOO_MANY_SUBS = '77505'    # subscribed too many times for this token (rate-limit)


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


class BinanceGateway:
    venue_id = 'binance'
    name = 'Binance'

    def __init__(self) -> None:
        self.spot = ccxt.binance({'apiKey': settings.binance_api_key, 'secret': settings.binance_api_secret, 'enableRateLimit': True})
        self.futures = ccxt.binanceusdm({'apiKey': settings.binance_api_key, 'secret': settings.binance_api_secret, 'enableRateLimit': True})
        self.last_balance_error: str = ''

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
            within = lambda p: p <= cutoff
        else:
            cutoff = mid * (1 - threshold)
            levels = bids
            within = lambda p: p >= cutoff
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

    _flexible_earn_apr_cache: dict[str, tuple[float, float]] = {}

    def flexible_earn_apr(self, asset: str, ttl_seconds: float = 3600.0) -> float:
        """Latest annualized rate (decimal) for the flexible Earn product on `asset`,
        cached for an hour. Returns 0.0 if no product or the API isn't reachable —
        caller can treat that as "no extra spot yield to count toward score."""
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

    def scan_funding(self, entry_apr_threshold: float, min_quote_volume: float, min_depth_usdt: float = 0.0, depth_band_bps: float = 10.0, include_earn_apr: bool = False) -> tuple[list[Candidate], int, list[tuple[str, str, float]]]:
        rates = self.futures.fetch_funding_rates()
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
            ))
        # Rank by total expected yield (funding APY + spot earn APR). Within
        # ties this naturally prefers the side with deeper books because deeper
        # markets tend to be the high-volume / low-friction names.
        passing.sort(key=lambda c: (c.combined_apy, c.min_depth_usdt), reverse=True)
        return passing, total, rejected

    def safe_balances(self) -> dict | None:
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
        """Minimum order quantity (LOT_SIZE.minQty) for a symbol. Binance refuses
        orders below this, so when our actual balance falls below it (e.g.,
        funding / fee deductions ate into the spot leg) the leg is effectively
        un-tradeable and the bot should treat it as flat rather than spinning
        on rejected close attempts. Returns 0.0 if the market isn't loaded — the
        caller should still attempt the order in that case."""
        ex = self.futures if perp else self.spot
        try:
            market = ex.market(symbol)
        except Exception:
            return 0.0
        try:
            return float((market.get('limits') or {}).get('amount', {}).get('min') or 0.0)
        except Exception:
            return 0.0

    def _market_order(self, venue: str, symbol: str, side: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        if paper_mode:
            mid = self.perp_price(symbol) if venue == 'futures' else self.price(symbol)
            slip = slippage_bps / 10000.0
            fill_price = mid * (1 + slip) if side == 'buy' else mid * (1 - slip)
            fee_cost = fill_price * amount * (fee_bps / 10000.0)
            return {'id': 'paper', 'symbol': symbol, 'side': side, 'amount': amount, 'venue': venue, 'status': 'closed', 'price': fill_price, 'fee': {'cost': fee_cost}}
        if venue == 'spot':
            return self.spot.create_order(symbol, 'market', side, amount)
        return self.futures.create_order(symbol, 'market', side, amount)

    def create_spot_buy(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        return self._market_order('spot', symbol, 'buy', amount, paper_mode, slippage_bps, fee_bps)

    def create_perp_short(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        return self._market_order('futures', symbol, 'sell', amount, paper_mode, slippage_bps, fee_bps)

    def close_spot(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        return self._market_order('spot', symbol, 'sell', amount, paper_mode, slippage_bps, fee_bps)

    def close_perp(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        return self._market_order('futures', symbol, 'buy', amount, paper_mode, slippage_bps, fee_bps)

    def open_perp_positions_raw(self) -> list[dict]:
        try:
            positions = self.futures.fetch_positions()
        except Exception:
            return []
        return [p for p in positions if abs(float(p.get('contracts') or p.get('info', {}).get('positionAmt') or 0)) > 0]

    # ─── Margin mode + leverage configuration ───────────────────────────────
    # For a delta-neutral arb, CROSS margin at 1x is the right setup:
    #  - CROSS shares margin across positions, avoiding single-leg liquidation
    #    risk on tiny price moves while the spot leg backs the perp short.
    #  - 1x leverage means the perp uses its full notional as margin, matching
    #    what we hold on the spot leg — keeps both legs symmetrically capitalized
    #    and prevents "Margin is insufficient" close failures from leverage drift.
    # Both calls are idempotent on Binance — repeating them when already-set is
    # cheap and safe.

    def configure_perp_for_arb(self, symbol: str) -> tuple[bool, str]:
        margin_ok = leverage_ok = True
        margin_err = leverage_err = ''
        try:
            self.futures.set_margin_mode('cross', symbol)
        except Exception as e:
            msg = str(e)
            # Binance returns "No need to change margin type" if it's already CROSS — that's fine.
            if 'No need to change' not in msg and 'no need to change' not in msg:
                margin_ok, margin_err = False, msg
        try:
            self.futures.set_leverage(1, symbol)
        except Exception as e:
            msg = str(e)
            if 'No need to change' not in msg and 'no need to change' not in msg:
                leverage_ok, leverage_err = False, msg
        if margin_ok and leverage_ok:
            return True, ''
        return False, '; '.join(filter(None, [margin_err, leverage_err]))

    # ─── Binance Simple Earn (Flexible USDT) ─────────────────────────────────
    # ccxt method names vary across versions; we probe a small set and call
    # the first that exists. All methods degrade gracefully — if the API key
    # lacks the Earn permission, the network is down, or the SAPI shape
    # changes, we return None / an error string and the caller leaves money
    # in the spot wallet.

    _earn_product_id_cache: dict[str, str] = {}

    def _call_sapi(self, candidates: tuple[str, ...], params: dict | None = None):
        for name in candidates:
            fn = getattr(self.spot, name, None)
            if callable(fn):
                return fn(params or {})
        return None

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

    # Backwards-compat alias used by callers that only deal with USDT.
    def earn_product_id_usdt(self) -> str | None:
        return self.earn_product_id('USDT')

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

    # Earn subscribe is rate-limited per (asset, day) on Binance — too many
    # subscribes returns 77505. We track the last attempt per asset and apply
    # a cooldown locally so we never spam Binance, and bump the cooldown if
    # Binance ever does return 77505.
    _earn_subscribe_cooldown_until: dict[str, float] = {}
    EARN_SUBSCRIBE_DEFAULT_COOLDOWN_S = 3600.0      # 1h between subscribes per asset
    EARN_SUBSCRIBE_RATE_LIMITED_COOLDOWN_S = 86400.0  # 24h after Binance signals 77505

    def earn_subscribe_asset(self, asset: str, amount: float, paper_mode: bool) -> tuple[bool, str]:
        """Subscribe `amount` of `asset` to Binance Simple Earn Flexible. Asset can be
        USDT or any base coin that has a flexible product (Binance offers flexible
        for most major listings). Caller must pre-format `amount` to the asset's
        precision; here we use 6 decimals as a conservative cap.

        Cooldown: each successful subscribe pins a per-asset cooldown so the
        bot doesn't spam the SAPI every 30s loop. 77505 from Binance bumps
        the cooldown to 24h for that asset."""
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
            return False, 'zero amount'
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
            return False, str(e)
        if resp is None:
            return False, 'sapi method not available in ccxt'
        return bool(resp.get('success', True)), ''

    # Convenience wrappers for the existing USDT-only callers.
    def earn_subscribe(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        return self.earn_subscribe_asset('USDT', amount_usdt, paper_mode)

    def earn_redeem(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        return self.earn_redeem_asset('USDT', amount_usdt, paper_mode)

    # ─── Universal transfer (spot ⇄ USDM-futures) ────────────────────────
    # Binance keeps spot and futures wallets separate; an arb bot needs USDT in
    # both. The universal transfer SAPI moves USDT between them instantly.

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

    # ─── Deposit / withdrawal history (for net-injected-capital headline) ───
    # Binance only returns up to 90 days of history per call, so we walk
    # backwards in 90-day chunks for `lookback_days` total. Cached for 5min.

    _deposit_history_cache: dict[str, tuple[list[dict], float]] = {}
    _withdrawal_history_cache: dict[str, tuple[list[dict], float]] = {}
    _sub_transfer_history_cache: dict[str, tuple[list[dict], float]] = {}

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
        type: 1 = transfer in (master → this sub), 2 = transfer out (this sub → master).
        Returns [] if the API key isn't on a sub-account or the endpoint isn't
        accessible (e.g., master keys hit a different endpoint)."""
        key = f'{asset}:{1 if incoming else 2}:{lookback_days}'
        cached = self._sub_transfer_history_cache.get(key)
        if cached and (time.time() - cached[1]) < ttl_seconds:
            return cached[0]
        rows: list[dict] = []
        # 30-day window per call on this endpoint historically.
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
        """Sum of all completed USDT inflows − outflows over the lookback window:
            inflows = external deposits + master→sub transfers (if sub-account)
            outflows = external withdrawals + sub→master transfers
        Returns (net_value, meta) where meta carries each component's count and
        total so the UI can show the breakdown. Each fetch is best-effort; if
        a particular endpoint isn't accessible (master key hitting sub endpoint
        and vice-versa) it just returns 0/empty for that component."""
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
        # If literally everything came back empty, signal failure so the caller
        # falls back to CapitalFlow / manual tracking instead of showing $0.
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
# Same surface as :class:`BinanceGateway` so the bot loop can iterate over a
# list of gateways without conditional logic. Significant differences vs
# Binance:
#   * Spot ↔ futures transfers go through KuCoin's ``innerTransfer`` SAPI on
#     the spot client (Binance uses ``sapiPostAssetTransfer``).
#   * Earn / Pool-X is deferred — KuCoin's flexible-lend product pays in the
#     deposited asset rather than USDT, breaking the "compounded APY in USDT"
#     accounting the bot uses today. earn_* methods all return safely-no-op.
#   * Net-injected-capital history is venue-specific and not yet implemented;
#     callers fall back to the manual CapitalFlow table.
#   * Symbol shape: ccxt normalises both KuCoin's perp suffix (e.g. ``XBTUSDTM``
#     → ``BTC/USDT:USDT``) and Binance's, so the bot's symbol handling is
#     unchanged.

class KuCoinGateway:
    venue_id = 'kucoin'
    name = 'KuCoin'

    def __init__(self) -> None:
        common = {
            'apiKey': settings.kucoin_api_key,
            'secret': settings.kucoin_api_secret,
            'password': settings.kucoin_api_passphrase,
            'enableRateLimit': True,
        }
        self.spot = ccxt.kucoin(common)
        self.futures = ccxt.kucoinfutures(common)
        self.last_balance_error: str = ''

    def load_markets(self) -> None:
        self.spot.load_markets()
        self.futures.load_markets()

    # ─── Read-only helpers ───────────────────────────────────────────────
    # These are ccxt-uniform across venues, so the implementations mirror
    # :class:`BinanceGateway` byte-for-byte. They exist on the gateway
    # rather than as free functions so the bot can pass a single object
    # into per-venue code paths.

    def order_book_depth_usdt(self, symbol: str, side: str = 'ask', band_bps: float = 10.0, perp: bool = False) -> float:
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
            within = lambda p: p <= cutoff
        else:
            cutoff = mid * (1 - threshold)
            levels = bids
            within = lambda p: p >= cutoff
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
        try:
            positions = self.futures.fetch_positions()
        except Exception:
            return []
        return [p for p in positions if abs(float(p.get('contracts') or p.get('info', {}).get('positionAmt') or 0)) > 0]

    # ─── Funding scan ───────────────────────────────────────────────────
    # KuCoin's fetch_funding_rates exposes the same shape as Binance's.
    # ccxt fills in interval, fundingRate, etc. We re-use _interval_hours
    # + annualize_rate from the module level so the math is consistent.

    def scan_funding(self, entry_apr_threshold: float, min_quote_volume: float, min_depth_usdt: float = 0.0, depth_band_bps: float = 10.0, include_earn_apr: bool = False) -> tuple[list[Candidate], int, list[tuple[str, str, float]]]:
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
            # KuCoin Earn / Pool-X deferred — pass 0.0 so the candidate's
            # combined_apy is just the funding APY for now.
            passing.append(Candidate(
                spot_symbol=spot_symbol,
                perp_symbol=symbol,
                funding_rate=float(fr),
                funding_interval_hours=interval_h,
                quote_volume=qv,
                spot_depth_usdt=spot_depth,
                perp_depth_usdt=perp_depth,
                spot_earn_apr=0.0,
            ))
        passing.sort(key=lambda c: (c.combined_apy, c.min_depth_usdt), reverse=True)
        return passing, total, rejected

    # ─── Order placement ────────────────────────────────────────────────

    def _market_order(self, venue: str, symbol: str, side: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        if paper_mode:
            mid = self.perp_price(symbol) if venue == 'futures' else self.price(symbol)
            slip = slippage_bps / 10000.0
            fill_price = mid * (1 + slip) if side == 'buy' else mid * (1 - slip)
            fee_cost = fill_price * amount * (fee_bps / 10000.0)
            return {'id': 'paper', 'symbol': symbol, 'side': side, 'amount': amount, 'venue': venue, 'status': 'closed', 'price': fill_price, 'fee': {'cost': fee_cost}}
        if venue == 'spot':
            return self.spot.create_order(symbol, 'market', side, amount)
        return self.futures.create_order(symbol, 'market', side, amount)

    def create_spot_buy(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        return self._market_order('spot', symbol, 'buy', amount, paper_mode, slippage_bps, fee_bps)

    def create_perp_short(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        return self._market_order('futures', symbol, 'sell', amount, paper_mode, slippage_bps, fee_bps)

    def close_spot(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        return self._market_order('spot', symbol, 'sell', amount, paper_mode, slippage_bps, fee_bps)

    def close_perp(self, symbol: str, amount: float, paper_mode: bool, slippage_bps: float, fee_bps: float):
        return self._market_order('futures', symbol, 'buy', amount, paper_mode, slippage_bps, fee_bps)

    # ─── Margin mode + leverage configuration ───────────────────────────
    # CROSS + 1x leverage matches Binance's setup. Both calls are
    # idempotent; KuCoin returns a benign error string when the symbol
    # is already on the requested setting.

    def configure_perp_for_arb(self, symbol: str) -> tuple[bool, str]:
        margin_ok = leverage_ok = True
        margin_err = leverage_err = ''
        try:
            self.futures.set_margin_mode('cross', symbol)
        except Exception as e:
            msg = str(e)
            if 'no need' not in msg.lower() and 'already' not in msg.lower():
                margin_ok, margin_err = False, msg
        try:
            self.futures.set_leverage(1, symbol)
        except Exception as e:
            msg = str(e)
            if 'no need' not in msg.lower() and 'already' not in msg.lower():
                leverage_ok, leverage_err = False, msg
        if margin_ok and leverage_ok:
            return True, ''
        return False, '; '.join(filter(None, [margin_err, leverage_err]))

    # ─── Spot ↔ futures transfer (KuCoin innerTransfer) ─────────────────

    def _inner_transfer(self, from_account: str, to_account: str, amount_usdt: float) -> tuple[bool, str]:
        # ccxt method names for KuCoin's transfer. We probe several
        # naming variants for ccxt-version drift, same pattern as the
        # SAPI lookup on Binance.
        for name in ('privatePostAccountsInnerTransfer', 'private_post_accounts_inner_transfer', 'privatePostAccountsUniversalTransfer'):
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

    # ─── Earn / Pool-X (deferred — Phase 1 doesn't subscribe on KuCoin) ─

    def earn_balance_usdt(self) -> tuple[float | None, str]:
        return 0.0, ''

    def earn_balance(self, asset: str = 'USDT') -> tuple[float | None, str]:
        return 0.0, ''

    def earn_subscribe(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, 'KuCoin Earn (Pool-X) not yet wired — see docs/kucoin-integration-plan.md'

    def earn_subscribe_asset(self, asset: str, amount: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, f'{asset}: KuCoin Earn (Pool-X) not yet wired'

    def earn_redeem(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, 'KuCoin Earn not subscribed'

    def earn_redeem_asset(self, asset: str, amount: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        return False, f'{asset}: KuCoin Earn not subscribed'

    def flexible_earn_apr(self, asset: str, ttl_seconds: float = 3600.0) -> float:
        return 0.0

    # ─── Capital-flow history (deferred — manual CapitalFlow only) ──────

    def net_injected_capital_usdt(self, lookback_days: int = 365) -> tuple[float | None, dict]:
        return None, {'error': 'KuCoin deposit/withdrawal history not yet wired'}


# ─── Multi-venue factory ────────────────────────────────────────────────
# The bot calls :func:`make_gateways` to discover which venues are live.
# A venue is "live" iff its credentials are present in the environment.
# Order matters: gateways earlier in the list are scanned first each
# cycle, so they get first pick of capital and candidates. Binance leads
# by default since it has the most mature integration and the most
# liquidity.

def make_gateways() -> list:
    """Return the ordered list of gateways the bot should drive this run.
    Empty list means no venue is configured — the bot loop becomes a
    no-op until credentials are set."""
    gws: list = []
    if settings.binance_api_key and settings.binance_api_secret:
        gws.append(BinanceGateway())
    if settings.kucoin_api_key and settings.kucoin_api_secret and settings.kucoin_api_passphrase:
        gws.append(KuCoinGateway())
    return gws
