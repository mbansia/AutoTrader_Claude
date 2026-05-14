"""Anomaly rules. Mirrors docs/SYSTEM.md §8.2.

Each rule is a pure function over the diagnostics snapshot.
"""

from __future__ import annotations

from typing import Any


def detect_anomalies(
    *,
    snapshot: dict[str, Any],
    error_count: int,
    cycle_count_estimate: int,
    cycle_error_rate_threshold: float,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    # no_recent_events: no BotEvent in last 3600s.
    sec = snapshot["cycle_health"]["seconds_since_last_event"]
    if sec is None or sec > 3600:
        out.append(
            {
                "severity": "critical",
                "rule": "no_recent_events",
                "detail": f"seconds_since_last_event={sec}",
            }
        )

    # stale_naked_*: naked older than 60min.
    for n in snapshot["positions"]["naked"]:
        age = n.get("age_minutes", 0.0)
        if age > 60.0:
            rule = (
                "stale_naked_spot"
                if n.get("status") == "naked_spot"
                else "stale_naked_perp"
            )
            out.append(
                {
                    "severity": "warn",
                    "rule": rule,
                    "detail": f"{n.get('symbol')} age={age:.0f}min",
                }
            )

    # no_trades_despite_scans: 0 trades AND > 20 rejections in window.
    if snapshot["recent_trades_count"] == 0 and snapshot["rejections_total"] > 20:
        out.append(
            {
                "severity": "warn",
                "rule": "no_trades_despite_scans",
                "detail": f"rejections={snapshot['rejections_total']}, trades=0",
            }
        )

    # error_burst
    if error_count > 20:
        out.append(
            {
                "severity": "warn",
                "rule": "error_burst",
                "detail": f"error_count={error_count}",
            }
        )

    # close_blocked: open position with non-empty last_close_error.
    for p in snapshot["positions"]["open"]:
        if p.get("last_close_error"):
            out.append(
                {
                    "severity": "warn",
                    "rule": "close_blocked",
                    "detail": f"{p['symbol']}: {p['last_close_error']}",
                }
            )

    # cycle_error_rate_high (v1.4)
    if cycle_count_estimate > 0:
        rate = error_count / cycle_count_estimate
        if rate > cycle_error_rate_threshold:
            out.append(
                {
                    "severity": "critical",
                    "rule": "cycle_error_rate_high",
                    "detail": f"rate={rate:.3f} threshold={cycle_error_rate_threshold:.3f}",
                }
            )

    return out
