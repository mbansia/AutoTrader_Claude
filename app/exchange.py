from __future__ import annotations

from dataclasses import dataclass

import ccxt

from app.config import settings


@dataclass
class Candidate:
    spot_symbol: str
    perp_symbol: str
    funding_rate: float
    quote_volume: float


class BinanceGateway:
    def __init__(self) -> None:
        self.spot = ccxt.binance({'apiKey': settings.binance_api_key, 'secret': settings.binance_api_secret, 'enableRateLimit': True})
        self.futures = ccxt.binanceusdm({'apiKey': settings.binance_api_key, 'secret': settings.binance_api_secret, 'enableRateLimit': True})

    def load_markets(self) -> None:
        self.spot.load_markets()
        self.futures.load_markets()

    def scan_funding(self, entry_threshold: float, min_quote_volume: float) -> tuple[list[Candidate], int, list[tuple[str, str, float]]]:
        rates = self.futures.fetch_funding_rates()
        passing: list[Candidate] = []
        rejected: list[tuple[str, str, float]] = []
        total = 0
        for symbol, row in rates.items():
            fr = row.get('fundingRate')
            if fr is None or not symbol.endswith(':USDT'):
                continue
            total += 1
            if fr < entry_threshold:
                continue
            base = symbol.split('/')[0]
            spot_symbol = f'{base}/USDT'
            if spot_symbol not in self.spot.markets:
                rejected.append((symbol, 'no_spot_market', float(fr)))
                continue
            try:
                t = self.spot.fetch_ticker(spot_symbol)
            except Exception:
                rejected.append((symbol, 'ticker_error', float(fr)))
                continue
            qv = float(t.get('quoteVolume') or 0)
            if qv < min_quote_volume:
                rejected.append((symbol, f'volume<{min_quote_volume:.0f}', float(fr)))
                continue
            passing.append(Candidate(spot_symbol=spot_symbol, perp_symbol=symbol, funding_rate=float(fr), quote_volume=qv))
        passing.sort(key=lambda c: c.funding_rate, reverse=True)
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
