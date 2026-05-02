from __future__ import annotations

from dataclasses import dataclass

import ccxt

from app.config import settings


@dataclass
class Candidate:
    spot_symbol: str
    perp_symbol: str
    funding_rate: float
    funding_interval_hours: float
    quote_volume: float

    @property
    def funding_apr(self) -> float:
        return annualize_rate(self.funding_rate, self.funding_interval_hours)


def annualize_rate(period_rate: float, interval_hours: float) -> float:
    """Convert a per-funding-period rate (decimal) to annualized (decimal). 8h interval ≈ 1095 periods/yr."""
    if interval_hours <= 0:
        return 0.0
    return period_rate * (24.0 * 365.0 / interval_hours)


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

    def load_markets(self) -> None:
        self.spot.load_markets()
        self.futures.load_markets()

    def scan_funding(self, entry_apr_threshold: float, min_quote_volume: float) -> tuple[list[Candidate], int, list[tuple[str, str, float]]]:
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
            passing.append(Candidate(spot_symbol=spot_symbol, perp_symbol=symbol, funding_rate=float(fr), funding_interval_hours=interval_h, quote_volume=qv))
        passing.sort(key=lambda c: c.funding_apr, reverse=True)
        return passing, total, rejected

    def safe_balances(self) -> dict | None:
        try:
            return {'spot': self.spot.fetch_balance(), 'futures': self.futures.fetch_balance()}
        except Exception:
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

    # ─── Binance Simple Earn (Flexible USDT) ─────────────────────────────────
    # ccxt method names vary across versions; we probe a small set and call
    # the first that exists. All methods degrade gracefully — if the API key
    # lacks the Earn permission, the network is down, or the SAPI shape
    # changes, we return None / an error string and the caller leaves money
    # in the spot wallet.

    _earn_product_id_cache: str | None = None

    def _call_sapi(self, candidates: tuple[str, ...], params: dict | None = None):
        for name in candidates:
            fn = getattr(self.spot, name, None)
            if callable(fn):
                return fn(params or {})
        return None

    def earn_product_id_usdt(self) -> str | None:
        if self._earn_product_id_cache:
            return self._earn_product_id_cache
        try:
            resp = self._call_sapi((
                'sapiV1GetSimpleEarnFlexibleList',
                'sapi_v1_get_simple_earn_flexible_list',
                'sapiGetSimpleEarnFlexibleList',
            ), {'asset': 'USDT'})
        except Exception:
            return None
        if not resp:
            return None
        rows = resp.get('rows') or resp.get('data') or []
        for r in rows:
            if r.get('asset') == 'USDT':
                pid = r.get('productId') or r.get('id')
                if pid:
                    self._earn_product_id_cache = str(pid)
                    return self._earn_product_id_cache
        return None

    def earn_balance_usdt(self) -> tuple[float | None, str]:
        try:
            resp = self._call_sapi((
                'sapiV1GetSimpleEarnFlexiblePosition',
                'sapi_v1_get_simple_earn_flexible_position',
                'sapiGetSimpleEarnFlexiblePosition',
            ), {'asset': 'USDT'})
        except Exception as e:
            return None, str(e)
        if resp is None:
            return None, 'sapi method not available in ccxt'
        total = 0.0
        for r in resp.get('rows', []) or []:
            total += float(r.get('totalAmount') or 0)
        return total, ''

    def earn_subscribe(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        pid = self.earn_product_id_usdt()
        if not pid:
            return False, 'no flexible USDT product id'
        try:
            resp = self._call_sapi((
                'sapiV1PostSimpleEarnFlexibleSubscribe',
                'sapi_v1_post_simple_earn_flexible_subscribe',
                'sapiPostSimpleEarnFlexibleSubscribe',
            ), {'productId': pid, 'amount': f'{amount_usdt:.2f}'})
        except Exception as e:
            return False, str(e)
        if resp is None:
            return False, 'sapi method not available in ccxt'
        return bool(resp.get('success', True)), ''

    def earn_redeem(self, amount_usdt: float, paper_mode: bool) -> tuple[bool, str]:
        if paper_mode:
            return True, 'paper'
        pid = self.earn_product_id_usdt()
        if not pid:
            return False, 'no flexible USDT product id'
        try:
            resp = self._call_sapi((
                'sapiV1PostSimpleEarnFlexibleRedeem',
                'sapi_v1_post_simple_earn_flexible_redeem',
                'sapiPostSimpleEarnFlexibleRedeem',
            ), {'productId': pid, 'amount': f'{amount_usdt:.2f}', 'destAccount': 'SPOT'})
        except Exception as e:
            return False, str(e)
        if resp is None:
            return False, 'sapi method not available in ccxt'
        return bool(resp.get('success', True)), ''
