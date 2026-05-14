"""Data-access helpers. Pure CRUD wrappers; no business logic.

Repository functions accept an open SQLAlchemy Session, never own it.
The caller's `session_scope()` is the unit of work.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select

from sqlalchemy.orm import Session

from . import models as m


# ─── singletons ────────────────────────────────────────────────────────────

def get_or_create_strategy_config(session: Session) -> m.StrategyConfig:
    cfg = session.get(m.StrategyConfig, 1)
    if cfg is None:
        cfg = m.StrategyConfig(id=1)
        session.add(cfg)
        session.flush()
    return cfg


def get_or_seed_per_strategy_config(
    session: Session, trade_type: str
) -> m.StrategyConfigPerStrategy:
    """Eager-seed per §3.1 mitigation policy (eliminates the lazy-seed race
    documented in §18). Caller should invoke at process start for each
    known trade_type so the first read isn't a race.
    """
    row = session.execute(
        select(m.StrategyConfigPerStrategy).where(
            m.StrategyConfigPerStrategy.trade_type == trade_type
        )
    ).scalar_one_or_none()
    if row is not None:
        return row
    # Seed from global config + class defaults (the SQLAlchemy column defaults
    # are aligned with §4 defaults).
    row = m.StrategyConfigPerStrategy(trade_type=trade_type)
    session.add(row)
    session.flush()
    return row


def get_or_create_mode_state(session: Session, mode: str) -> m.ModeState:
    row = session.get(m.ModeState, mode)
    if row is None:
        row = m.ModeState(mode=mode)
        session.add(row)
        session.flush()
    return row


def get_or_create_strategy_state(
    session: Session, mode: str, trade_type: str
) -> m.StrategyState:
    key = (mode, trade_type)
    row = session.get(m.StrategyState, key)
    if row is None:
        row = m.StrategyState(mode=mode, trade_type=trade_type)
        session.add(row)
        session.flush()
    return row


# ─── positions ─────────────────────────────────────────────────────────────

OPEN_STATUSES_SQL = ("open", "naked_spot", "naked_perp")


def positions_currently_exposed(
    session: Session, mode: str, exchange: str | None = None
) -> list[m.Position]:
    q = select(m.Position).where(
        m.Position.mode == mode, m.Position.status.in_(OPEN_STATUSES_SQL)
    )
    if exchange is not None:
        q = q.where(m.Position.exchange == exchange)
    return list(session.scalars(q))


def positions_by_status(session: Session, mode: str | None = None) -> dict[str, int]:
    q = select(m.Position)
    if mode is not None:
        q = q.where(m.Position.mode == mode)
    result: dict[str, int] = {}
    for p in session.scalars(q):
        result[p.status] = result.get(p.status, 0) + 1
    return result


def insert_position(session: Session, pos: m.Position) -> m.Position:
    session.add(pos)
    session.flush()
    return pos


def insert_trade(session: Session, trade: m.Trade) -> m.Trade:
    session.add(trade)
    session.flush()
    return trade


def log_event(
    session: Session,
    *,
    mode: str,
    exchange: str,
    level: str,
    message: str,
    requires_action: bool = False,
) -> m.BotEvent:
    ev = m.BotEvent(
        mode=mode,
        exchange=exchange,
        level=level,
        message=message,
        requires_action=requires_action,
    )
    session.add(ev)
    session.flush()
    return ev


def insert_rejection(
    session: Session,
    *,
    mode: str,
    exchange: str,
    symbol: str,
    reason: str,
    funding_rate: float = 0.0,
) -> m.RejectedCandidate:
    r = m.RejectedCandidate(
        mode=mode,
        exchange=exchange,
        symbol=symbol,
        reason=reason,
        funding_rate=funding_rate,
    )
    session.add(r)
    session.flush()
    return r


def recent_events(
    session: Session, *, hours: int, levels: Iterable[str] | None = None, limit: int = 50
) -> list[m.BotEvent]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = (
        select(m.BotEvent)
        .where(m.BotEvent.ts >= cutoff)
        .order_by(m.BotEvent.ts.desc())
        .limit(limit)
    )
    if levels is not None:
        q = q.where(m.BotEvent.level.in_(list(levels)))
    return list(session.scalars(q))


def event_counts_in_window(session: Session, hours: int) -> tuple[int, int, int]:
    """Returns (info, warn, error) counts. Used by §8.2 anomaly rules."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    info = warn = err = 0
    for ev in session.scalars(select(m.BotEvent).where(m.BotEvent.ts >= cutoff)):
        if ev.level == "INFO":
            info += 1
        elif ev.level == "WARN":
            warn += 1
        elif ev.level == "ERROR":
            err += 1
    return info, warn, err


def recent_trades(session: Session, hours: int, limit: int = 50) -> list[m.Trade]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    q = (
        select(m.Trade)
        .where(m.Trade.ts >= cutoff)
        .order_by(m.Trade.ts.desc())
        .limit(limit)
    )
    return list(session.scalars(q))


def rejections_grouped(session: Session, hours: int) -> tuple[int, dict[str, dict[str, int]]]:
    """Returns (total_rejections, grouped_by_venue_mode_then_category).
    Category = the prefix before the first colon or paren in `reason`.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    grouped: dict[str, dict[str, int]] = {}
    total = 0
    for r in session.scalars(
        select(m.RejectedCandidate).where(m.RejectedCandidate.ts >= cutoff)
    ):
        total += 1
        bucket_key = f"{r.exchange}/{r.mode}"
        cat = r.reason
        for sep in (":", "("):
            if sep in cat:
                cat = cat.split(sep, 1)[0]
                break
        cat = cat.strip()
        grouped.setdefault(bucket_key, {})
        grouped[bucket_key][cat] = grouped[bucket_key].get(cat, 0) + 1
    return total, grouped
