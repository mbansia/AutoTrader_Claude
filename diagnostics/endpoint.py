"""`/api/diagnostics` JSON snapshot. Shape is frozen per docs/SYSTEM.md §8.1.

Pure function from (Session, hours) → dict. The HTTP layer in `web/` is a
thin auth + serialization wrapper around `build_snapshot()`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.types import OPEN_STATUSES, PositionStatus
from state import models as m
from state import repository as repo

from .anomalies import detect_anomalies


def _iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _clamp(hours: int) -> int:
    if hours < 1:
        return 1
    if hours > 168:
        return 168
    return hours


def build_snapshot(session: Session, hours_raw: int = 24) -> dict[str, Any]:
    """Build the JSON snapshot per §8.1."""
    hours = _clamp(hours_raw)
    now_utc = datetime.now(timezone.utc)

    info, warn, err = repo.event_counts_in_window(session, hours)
    last_event = session.execute(
        select(m.BotEvent).order_by(m.BotEvent.ts.desc()).limit(1)
    ).scalar_one_or_none()

    seconds_since = None
    last_msg = None
    if last_event is not None:
        ts = last_event.ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        seconds_since = (now_utc - ts).total_seconds()
        last_msg = _truncate(last_event.message, 240)

    open_rows: list[dict[str, Any]] = []
    naked_rows: list[dict[str, Any]] = []
    by_status: dict[str, int] = {"open": 0, "naked_spot": 0, "naked_perp": 0, "closed": 0}

    for p in session.scalars(select(m.Position)):
        by_status[p.status] = by_status.get(p.status, 0) + 1
        if p.status == PositionStatus.OPEN.value:
            age_hours = _age_hours(p.opened_at, now_utc)
            open_rows.append(
                {
                    "id": p.id,
                    "mode": p.mode,
                    "exchange": p.exchange,
                    "symbol": p.symbol,
                    "perp_symbol": p.perp_symbol,
                    "quote_currency": p.quote_currency,
                    "quantity": p.quantity,
                    "spot_entry_price": p.spot_entry_price,
                    "perp_entry_price": p.perp_entry_price,
                    "last_funding_rate": p.last_funding_rate,
                    "funding_interval_h": p.funding_interval_hours,
                    "last_close_error": p.last_close_error,
                    "age_hours": age_hours,
                }
            )
        elif p.status in {PositionStatus.NAKED_SPOT.value, PositionStatus.NAKED_PERP.value}:
            leg_price = (
                p.spot_entry_price
                if p.status == PositionStatus.NAKED_SPOT.value
                else p.perp_entry_price
            )
            naked_rows.append(
                {
                    "id": p.id,
                    "mode": p.mode,
                    "exchange": p.exchange,
                    "status": p.status,
                    "symbol": p.symbol,
                    "spot_symbol": p.spot_symbol if p.status == PositionStatus.NAKED_SPOT.value else "",
                    "perp_symbol": p.perp_symbol if p.status == PositionStatus.NAKED_PERP.value else "",
                    "quantity": p.quantity,
                    "leg_entry_price": leg_price,
                    "notional_est": p.quantity * leg_price,
                    "age_minutes": _age_minutes(p.opened_at, now_utc),
                }
            )

    rec_events = []
    for ev in repo.recent_events(session, hours=hours, levels=("WARN", "ERROR")):
        rec_events.append(
            {
                "ts": _iso_z(ev.ts),
                "level": ev.level,
                "exchange": ev.exchange,
                "mode": ev.mode,
                "msg": _truncate(ev.message, 400),
            }
        )

    rec_trades = []
    for t in repo.recent_trades(session, hours=hours):
        rec_trades.append(
            {
                "ts": _iso_z(t.ts),
                "mode": t.mode,
                "exchange": t.exchange,
                "symbol": t.symbol,
                "venue_leg": t.venue,
                "side": t.side,
                "qty": t.quantity,
                "price": t.price,
                "fee": t.fee,
            }
        )

    total_rejections, grouped = repo.rejections_grouped(session, hours=hours)

    # Approximate wallets fixture: real gateways provide this; the diag
    # endpoint pulls the latest balance_snapshots when available.
    wallets: dict[str, Any] = {}

    snapshot: dict[str, Any] = {
        "generated_at_utc": _iso_z(now_utc),
        "window_hours": hours,
        "cycle_health": {
            "last_event_ts": _iso_z(last_event.ts) if last_event is not None else None,
            "last_event_msg": last_msg,
            "seconds_since_last_event": seconds_since,
            "error_count": err,
            "warn_count": warn,
        },
        "positions": {
            "by_status": by_status,
            "open": open_rows,
            "naked": naked_rows,
        },
        "wallets": wallets,
        "rejections_grouped": grouped,
        "rejections_total": total_rejections,
        "recent_events": rec_events,
        "recent_trades": rec_trades,
        "recent_trades_count": len(rec_trades),
        "anomalies": [],
        "anomalies_count": 0,
    }

    cfg = repo.get_or_create_strategy_config(session)
    cycle_count_estimate = _approx_cycles_in_window(hours, cfg.loop_seconds)
    anomalies = detect_anomalies(
        snapshot=snapshot,
        error_count=err,
        cycle_count_estimate=cycle_count_estimate,
        cycle_error_rate_threshold=cfg.cycle_error_rate_threshold,
    )
    snapshot["anomalies"] = anomalies
    snapshot["anomalies_count"] = len(anomalies)
    return snapshot


def _age_hours(opened_at: datetime, now: datetime) -> float:
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    return (now - opened_at).total_seconds() / 3600.0


def _age_minutes(opened_at: datetime, now: datetime) -> float:
    if opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=timezone.utc)
    return (now - opened_at).total_seconds() / 60.0


def _approx_cycles_in_window(hours: int, loop_seconds: int) -> int:
    if loop_seconds <= 0:
        return 0
    return int((hours * 3600) / loop_seconds)
