from __future__ import annotations

import time
from dataclasses import dataclass, field

import ccxt

from app.config import settings


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

    def earn_subscribe_asset(self, asset: str, amount: float, paper_mode: bool) -> tuple[bool, str]:
        """Subscribe `amount` of `asset` to Binance Simple Earn Flexible. Asset can be
        USDT or any base coin that has a flexible product (Binance offers flexible
        for most major listings). Caller must pre-format `amount` to the asset's
        precision; here we use 6 decimals as a conservative cap."""
        if paper_mode:
            return True, 'paper'
        if amount <= 0:
            return False, 'zero amount'
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
            return False, str(e)
        if resp is None:
            return False, 'sapi method not available in ccxt'
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

    def net_injected_capital_usdt(self, lookback_days: int = 365) -> tuple[float | None, dict]:
        """Sum of completed USDT deposits − completed USDT withdrawals over the
        lookback window. Returns (net_value, meta) where meta carries deposit/
        withdraw totals and counts so the UI can show the breakdown. Returns
        (None, …) if the API call failed (caller falls back to CapitalFlow rows)."""
        try:
            deps = self.deposit_history('USDT', lookback_days=lookback_days)
            wdrs = self.withdrawal_history('USDT', lookback_days=lookback_days)
        except Exception as e:
            return None, {'error': str(e)[:120]}
        total_in = sum(float(d.get('amount') or 0) for d in deps)
        total_out = sum(float(w.get('amount') or 0) for w in wdrs)
        return total_in - total_out, {
            'deposits_count': len(deps),
            'deposits_total': total_in,
            'withdrawals_count': len(wdrs),
            'withdrawals_total': total_out,
            'asset': 'USDT',
            'lookback_days': lookback_days,
        }
