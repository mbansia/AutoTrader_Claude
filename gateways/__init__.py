"""Venue-adapter layer. Each module implements the `Gateway` protocol.

`InMemoryGateway` powers paper mode + tests. `BinanceGateway` and
`KuCoinGateway` are ccxt-backed live adapters per §6.1/§6.2 wallet models
and §16 L11 quirks. Live cutover validation is §17 Stage 5.
"""

from .base import Gateway
from .binance import BinanceGateway
from .kucoin import KuCoinGateway
from .paper import InMemoryGateway


def build_live_gateway(exchange_id: str, *, env):
    """Factory: construct a live gateway from env-loaded creds.

    `env` is a `core.config.EnvConfig` instance.
    """
    if exchange_id == "binance":
        return BinanceGateway(
            api_key=env.binance_api_key,
            api_secret=env.binance_api_secret,
            expected_account_id=env.binance_expected_account_id,
        )
    if exchange_id == "kucoin":
        return KuCoinGateway(
            api_key=env.kucoin_api_key,
            api_secret=env.kucoin_api_secret,
            passphrase=env.kucoin_passphrase,
            expected_account_id=env.kucoin_expected_account_id,
        )
    raise ValueError(f"unknown exchange: {exchange_id}")
