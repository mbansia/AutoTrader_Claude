"""In-process event bus for the bot loop.

Lightweight pub/sub so cycle steps that don't need to run every iteration
(earn sweep, capital-flow ingest) can react to *real* triggers instead of
polling. The previous design ran every step every cycle, which on KuCoin's
per-second rate limits triggered 429s and on Binance's earn cooldown
caused silent lockouts. Events let the work happen exactly when it
matters.

Design choices:
* In-process singleton — the bot worker, the FastAPI request handlers,
  and any background tasks all share one instance. No cross-process
  coordination needed at this scale.
* Synchronous handlers — keeps the call graph simple and avoids
  threading bugs in our SQLAlchemy session lifetime. If a handler is
  slow (e.g. a SAPI call), it blocks the emitter; that's fine because
  emitters are themselves running off the once-per-30s cycle.
* Best-effort: a handler that raises is logged and the bus moves on
  to the next handler. One bad subscriber must not break the cycle.

Events the bot currently emits:
* ``position_opened``  — after a new position row is committed.
  payload: {position_id, mode, exchange, symbol, quantity, notional}
* ``position_closed``  — after both legs of a position close cleanly.
  payload: {position_id, mode, exchange, symbol, realized_usdt}
* ``deposit_detected`` — after capital-flow ingest finds a new inflow.
  payload: {mode, exchange, amount_usdt, kind, ts}

Handler registration usually happens at module import time so handlers
are always registered before the cycle loop starts.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

logger = logging.getLogger(__name__)

EventHandler = Callable[..., None]


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event: str, handler: EventHandler) -> None:
        if handler not in self._subs[event]:
            self._subs[event].append(handler)

    def unsubscribe(self, event: str, handler: EventHandler) -> None:
        if handler in self._subs[event]:
            self._subs[event].remove(handler)

    def emit(self, event: str, **payload: Any) -> None:
        for handler in list(self._subs.get(event, ())):
            try:
                handler(**payload)
            except Exception as e:
                logger.exception('event handler %r failed for %s: %s', handler, event, e)


# Module-level singleton. Import this in bot.py / main.py.
bus = EventBus()
