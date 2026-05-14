"""Shared ccxt helpers — error mapping, metadata caching, idempotency.

Per §3.1 mitigation policy: cache market metadata per-cycle with reject-driven
refresh, dedup repeated identical errors, and isolate per-leg exceptions.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.types import BookLevel, BookSnapshot, FillResult, Side, VenueLeg, utcnow


log = logging.getLogger(__name__)


def book_from_ccxt(orderbook: dict[str, Any], symbol: str) -> BookSnapshot:
    """Convert ccxt's `fetch_order_book` payload to our BookSnapshot.

    ccxt yields:
      { 'bids': [[price, amount], ...],  # descending
        'asks': [[price, amount], ...],  # ascending
        'timestamp': int(ms) }
    """
    asks = [BookLevel(price=float(p), depth=float(a)) for p, a in orderbook.get("asks", [])]
    bids = [BookLevel(price=float(p), depth=float(a)) for p, a in orderbook.get("bids", [])]
    mid = 0.0
    if asks and bids:
        mid = (asks[0].price + bids[0].price) / 2.0
    ts = orderbook.get("timestamp")
    if ts is None:
        taken_at = utcnow()
    else:
        from datetime import datetime, timezone
        taken_at = datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc)
    return BookSnapshot(symbol=symbol, bids=bids, asks=asks, mid_price=mid, taken_at=taken_at)


def order_to_fill_result(
    ccxt_order: dict[str, Any] | None,
    *,
    symbol: str,
    leg: VenueLeg,
    side: Side,
    coid: str,
) -> FillResult:
    """Map a ccxt order response to FillResult.

    ccxt order shape (post-fill):
      { 'status': 'closed' | 'canceled' | 'rejected',
        'filled': float, 'average': float, 'fee': {'cost': float, 'currency': str} }
    """
    if ccxt_order is None:
        return FillResult(
            symbol=symbol, venue_leg=leg, side=side, qty=0.0,
            avg_price=0.0, fee_quote=0.0, accepted=False,
            error="empty_response", client_order_id=coid,
        )
    status = (ccxt_order.get("status") or "").lower()
    filled = float(ccxt_order.get("filled") or 0.0)
    avg = float(ccxt_order.get("average") or 0.0)
    fee_obj = ccxt_order.get("fee") or {}
    fee_quote = float(fee_obj.get("cost") or 0.0)
    if status in {"closed", "filled"} and filled > 0.0:
        return FillResult(
            symbol=symbol, venue_leg=leg, side=side, qty=filled,
            avg_price=avg, fee_quote=fee_quote, accepted=True,
            error=None, client_order_id=coid,
        )
    return FillResult(
        symbol=symbol, venue_leg=leg, side=side, qty=0.0,
        avg_price=0.0, fee_quote=0.0, accepted=False,
        error=f"fok_rejected:{status}", client_order_id=coid,
    )


class ErrorDedup:
    """L20: identical-message error dedup per pattern."""

    def __init__(self, window_seconds: int = 60) -> None:
        self._window = window_seconds
        self._seen: dict[str, float] = {}

    def should_log(self, key: str) -> bool:
        now = time.time()
        last = self._seen.get(key)
        if last is not None and (now - last) < self._window:
            return False
        self._seen[key] = now
        return True
