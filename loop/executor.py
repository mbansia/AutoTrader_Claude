"""Order placement: closed-form sizing → parallel market+FOK → rollback on
single-leg orphan. Implements §3.1 Phase C entries + §3.1 mitigation policy.

`place_entry()` returns one of three outcomes:
  - SUCCESS: both legs filled, position persisted as `open`.
  - REJECTED: clean reject (both legs rejected, or sizing failed). No state changes.
  - SINGLE_LEG_ORPHAN: one leg filled, rollback attempted.
    - rollback succeeded → no naked row; return rejected with `single_leg_orphan` reason.
    - rollback failed → naked row persisted; phantom-recovery picks up next cycle.

Parallel placement is genuine concurrency: both orders fire via
`concurrent.futures.ThreadPoolExecutor` and we wait on both before
classifying the outcome. ccxt is sync; the threads serialize the network
I/O at the OS level so the inter-leg gap is bounded by the slower call,
not the sum.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from typing import Literal

from gateways.base import Gateway

from core.types import FillResult, Side


class EntryOutcome(str, Enum):
    SUCCESS = "success"
    REJECTED = "rejected"
    SINGLE_LEG_ORPHAN_RECOVERED = "single_leg_orphan_recovered"
    SINGLE_LEG_ORPHAN_NAKED = "single_leg_orphan_naked"


@dataclass(slots=True)
class EntryResult:
    outcome: EntryOutcome
    spot_fill: FillResult | None
    perp_fill: FillResult | None
    rollback_fill: FillResult | None = None
    reason: str = ""


def _place(
    gateway: Gateway,
    symbol: str,
    leg: Literal["spot", "futures"],
    side: Side,
    qty: float,
    coid: str,
) -> FillResult:
    """Per-leg call: any exception becomes a non-accepted FillResult so the
    cycle doesn't crash on a transient single-leg failure (§3.1 mitigation).
    """
    try:
        return gateway.place_market_fok(symbol, leg, side, qty, coid)
    except Exception as exc:  # noqa: BLE001
        return FillResult(
            symbol=symbol,
            venue_leg=leg,
            side=side,
            qty=0.0,
            avg_price=0.0,
            fee_quote=0.0,
            accepted=False,
            error=f"exception:{type(exc).__name__}:{exc}",
            client_order_id=coid,
        )


def parallel_market_fok_entry(
    gateway: Gateway,
    *,
    spot_symbol: str,
    perp_symbol: str,
    qty: float,
    spot_coid: str,
    perp_coid: str,
    leg_timeout_s: float = 15.0,
) -> EntryResult:
    """Submit both legs concurrently. Classify outcome by which fills landed.

    Entry direction: buy spot + sell perp short.

    `leg_timeout_s` bounds the wait per leg. If a venue hangs, the future is
    abandoned and we treat the leg as failed; the cycle continues. Avoids
    the deadlock-class failure if a gateway never returns.
    """
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="leg") as pool:
        f_spot = pool.submit(
            _place, gateway, spot_symbol, "spot", "buy", qty, spot_coid
        )
        f_perp = pool.submit(
            _place, gateway, perp_symbol, "futures", "sell", qty, perp_coid
        )
        fills: dict[str, FillResult] = {}
        try:
            for fut in as_completed([f_spot, f_perp], timeout=leg_timeout_s):
                r = fut.result()
                fills[r.venue_leg] = r
        except TimeoutError:
            for leg_name, fut, sym, side in (
                ("spot", f_spot, spot_symbol, "buy"),
                ("futures", f_perp, perp_symbol, "sell"),
            ):
                if leg_name not in fills:
                    fut.cancel()
                    fills[leg_name] = FillResult(
                        symbol=sym,
                        venue_leg=leg_name,
                        side=side,
                        qty=0.0,
                        avg_price=0.0,
                        fee_quote=0.0,
                        accepted=False,
                        error="leg_timeout",
                        client_order_id=spot_coid if leg_name == "spot" else perp_coid,
                    )

    spot = fills.get("spot")
    perp = fills.get("futures")

    if spot is not None and perp is not None and spot.accepted and perp.accepted:
        return EntryResult(
            outcome=EntryOutcome.SUCCESS,
            spot_fill=spot,
            perp_fill=perp,
            reason="",
        )

    if (spot is None or not spot.accepted) and (perp is None or not perp.accepted):
        return EntryResult(
            outcome=EntryOutcome.REJECTED,
            spot_fill=spot,
            perp_fill=perp,
            reason=_combine_reasons(spot, perp),
        )

    # Exactly one leg filled — attempt same-cycle rollback.
    filled, missing = (spot, perp) if (spot and spot.accepted) else (perp, spot)
    rollback_symbol = filled.symbol
    rollback_leg: Literal["spot", "futures"] = filled.venue_leg
    rollback_side: Side = "sell" if filled.side == "buy" else "buy"
    rollback_coid = f"r-{filled.client_order_id}"
    rollback = gateway.place_market_fok(
        rollback_symbol, rollback_leg, rollback_side, filled.qty, rollback_coid
    )

    if rollback.accepted:
        return EntryResult(
            outcome=EntryOutcome.SINGLE_LEG_ORPHAN_RECOVERED,
            spot_fill=spot,
            perp_fill=perp,
            rollback_fill=rollback,
            reason="single_leg_orphan_rolled_back",
        )
    return EntryResult(
        outcome=EntryOutcome.SINGLE_LEG_ORPHAN_NAKED,
        spot_fill=spot,
        perp_fill=perp,
        rollback_fill=rollback,
        reason="single_leg_orphan_rollback_failed",
    )


def _combine_reasons(spot: FillResult | None, perp: FillResult | None) -> str:
    parts = []
    if spot is not None and spot.error:
        parts.append(f"spot:{spot.error}")
    if perp is not None and perp.error:
        parts.append(f"perp:{perp.error}")
    return "fok_rejected:" + "+".join(parts) if parts else "fok_rejected:unknown"
