from __future__ import annotations

from dataclasses import dataclass

import ccxt

from app.config import ENTRY_FUNDING_THRESHOLD, MIN_24H_QUOTE_VOLUME, settings


@dataclass
class Candidate:
    spot_symbol: str
    perp_symbol: str
    funding_rate: float


class BinanceGateway:
    def __init__(self) -> None:
        self.spot = ccxt.binance({'apiKey': settings.binance_api_key, 'secret': settings.binance_api_secret, 'enableRateLimit': True})
        self.futures = ccxt.binanceusdm({'apiKey': settings.binance_api_key, 'secret': settings.binance_api_secret, 'enableRateLimit': True})

    def load_markets(self) -> None:
        self.spot.load_markets()
        self.futures.load_markets()

    def top_funding_candidates(self) -> list[Candidate]:
        rates = self.futures.fetch_funding_rates()
        out: list[Candidate] = []
        for symbol, row in rates.items():
            fr = row.get('fundingRate')
            if fr is None or fr < ENTRY_FUNDING_THRESHOLD or not symbol.endswith(':USDT'):
                continue
            base = symbol.split('/')[0]
            spot_symbol = f'{base}/USDT'
            if spot_symbol not in self.spot.markets:
                continue
            t = self.spot.fetch_ticker(spot_symbol)
            if float(t.get('quoteVolume') or 0) < MIN_24H_QUOTE_VOLUME:
                continue
            out.append(Candidate(spot_symbol=spot_symbol, perp_symbol=symbol, funding_rate=float(fr)))
        return sorted(out, key=lambda c: c.funding_rate, reverse=True)

    def balances(self) -> dict:
        return {'spot': self.spot.fetch_balance(), 'futures': self.futures.fetch_balance()}

    def price(self, symbol: str) -> float:
        return float(self.spot.fetch_ticker(symbol)['last'])

    def _market_order(self, venue: str, symbol: str, side: str, amount: float, paper_mode: bool):
        if paper_mode:
            return {'id': 'paper', 'symbol': symbol, 'side': side, 'amount': amount, 'venue': venue, 'status': 'closed', 'price': self.price(symbol.split(':')[0] if ':' in symbol else symbol), 'fee': {'cost': 0.0}}
        if venue == 'spot':
            return self.spot.create_order(symbol, 'market', side, amount)
        return self.futures.create_order(symbol, 'market', side, amount)

    def create_spot_buy(self, symbol: str, amount: float, paper_mode: bool):
        return self._market_order('spot', symbol, 'buy', amount, paper_mode)

    def create_perp_short(self, symbol: str, amount: float, paper_mode: bool):
        return self._market_order('futures', symbol, 'sell', amount, paper_mode)

    def close_spot(self, symbol: str, amount: float, paper_mode: bool):
        return self._market_order('spot', symbol, 'sell', amount, paper_mode)

    def close_perp(self, symbol: str, amount: float, paper_mode: bool):
        return self._market_order('futures', symbol, 'buy', amount, paper_mode)

    def open_perp_positions_raw(self) -> list[dict]:
        positions = self.futures.fetch_positions()
        return [p for p in positions if abs(float(p.get('contracts') or p.get('info', {}).get('positionAmt') or 0)) > 0]
