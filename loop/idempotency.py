"""Deterministic client-order-ids (§16 L43).

A client-order-id is a stable function of (cycle_id, candidate_symbol, leg).
A network-retry of the same call carries the same id, so the venue
deduplicates server-side and we never accidentally double-place an order.

Cycle ids are short, monotonic per process; they're also embedded in the
position lifecycle for audit.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Literal


def make_cycle_id(epoch: float | None = None) -> str:
    """Short monotonic id for a cycle. Encodes the current epoch time to
    millisecond resolution + a per-process random suffix to disambiguate
    parallel mode runs (paper + live)."""
    e = epoch if epoch is not None else time.time()
    suffix = os.urandom(2).hex()
    return f"{int(e * 1000)}-{suffix}"


def client_order_id(
    cycle_id: str,
    venue_symbol: str,
    leg: Literal["spot", "futures"],
    side: Literal["buy", "sell"],
) -> str:
    """Deterministic. The same (cycle, symbol, leg, side) always produces the
    same id, so a retry of an order call is idempotent at the venue.

    Length-bounded: most venues cap client-order-id to 32-36 chars. We hash
    the composition and prefix with a short tag for grep-ability.
    """
    composite = f"{cycle_id}|{venue_symbol}|{leg}|{side}"
    h = hashlib.sha256(composite.encode("utf-8")).hexdigest()[:24]
    tag = "c"
    return f"{tag}{h}"
