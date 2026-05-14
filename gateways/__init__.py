"""Venue-adapter layer. Each module implements the `Gateway` protocol.

Today: `InMemoryGateway` (paper / tests). `BinanceGateway` and `KuCoinGateway`
are protocol stubs awaiting ccxt-credentialed integration in the live cutover.
"""

from .base import Gateway
from .paper import InMemoryGateway
