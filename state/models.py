"""SQLAlchemy 2.x models. Schema matches docs/SYSTEM.md §7.1.1 frozen contract.

Additive-only migrations live in `state.db.run_migrations`.
Datetime columns are stored UTC-aware (§7.4 timestamp discipline).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class StrategyConfig(Base):
    """Singleton (id=1). Global account/process/mode-level fields. §4."""

    __tablename__ = "strategy_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_trades_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    loop_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    paper_starting_equity: Mapped[float] = mapped_column(Float, nullable=False, default=1000.0)
    paper_slippage_bps: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    paper_fee_bps: Mapped[float] = mapped_column(Float, nullable=False, default=4.0)
    hedge_integrity_check: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    delisting_check: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    cycle_error_rate_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.10)
    slippage_alert_bps: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class StrategyConfigPerStrategy(Base):
    """One row per active trade_type. Per-strategy fields override global. §4."""

    __tablename__ = "strategy_config_per_strategy"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_type: Mapped[str] = mapped_column(String(48), nullable=False, unique=True)
    entry_min_net_apy: Mapped[float] = mapped_column(Float, nullable=False, default=0.20)
    exit_min_net_apy: Mapped[float] = mapped_column(Float, nullable=False, default=0.05)
    exit_basis_buffer_multiple: Mapped[float] = mapped_column(Float, nullable=False, default=3.0)
    max_exit_basis_bps: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)
    stop_loss_pct: Mapped[float] = mapped_column(Float, nullable=False, default=-0.02)
    basis_dislocation_exit_bps: Mapped[float] = mapped_column(Float, nullable=False, default=50.0)
    # Deprecated v1.3 (kept per additive-only policy):
    max_hold_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=72)
    min_position_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.005)
    max_position_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.10)
    sub_target_sizing_factor: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)
    # Deprecated v1.4 (kept per additive-only policy):
    entry_tick_buffer_bps: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    exit_tick_buffer_bps: Mapped[float] = mapped_column(Float, nullable=False, default=2.0)
    perp_leverage: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_perp_leverage: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    auto_transfer_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    auto_quote_swap_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    futures_buffer_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.20)
    depeg_guard_bps: Mapped[float] = mapped_column(Float, nullable=False, default=25.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ModeState(Base):
    __tablename__ = "mode_state"
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    entry_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    exit_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class StrategyState(Base):
    __tablename__ = "strategy_state"
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    trade_type: Mapped[str] = mapped_column(String(48), primary_key=True)
    entry_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    exit_all_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class VenueState(Base):
    """Per-venue activation + the wallet-id assertion lock. Replaces the
    `ACTIVE_EXCHANGES` env var (v1.6). Operator toggles via `/safety`.
    Defaults: binance + kucoin active=true; hyperliquid active=false (opt-in).
    """

    __tablename__ = "venue_state"
    exchange_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expected_account_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class Position(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="binance")
    trade_type: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        index=True,
        default="binance_same_venue_funding_arb",
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    spot_symbol: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    perp_symbol: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    quote_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USDT", index=True)
    spot_quote_currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="USDT", index=True
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    entry_funding_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_funding_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    spot_entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    perp_entry_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    funding_interval_hours: Mapped[float] = mapped_column(Float, nullable=False, default=8.0)
    funding_income_accrued: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_funding_accrual_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_close_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="binance")
    trade_type: Mapped[str] = mapped_column(
        String(48),
        nullable=False,
        index=True,
        default="binance_same_venue_funding_arb",
    )
    position_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("positions.id"), nullable=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    venue: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)


class BotEvent(Base):
    __tablename__ = "bot_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="system")
    level: Mapped[str] = mapped_column(String(8), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    requires_action: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class RejectedCandidate(Base):
    __tablename__ = "rejected_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="binance")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    funding_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="binance")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    spot_free_usdt: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    futures_free_usdt: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_equity_usdt: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # ─── Legacy v1.3 column shims — read-only, never written by v1.5 ───
    # Pre-rewrite schema named these `spot_usdt`, `futures_usdt`,
    # `total_usdt`, `source`. Declared here so dashboard reads can fall
    # back when the v1.5-named columns are at their default 0.0 (legacy
    # rows). Additive-only policy (§7.3) preserves them on the table;
    # this declaration just makes them visible to the ORM.
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="paper")
    spot_usdt: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    futures_usdt: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    total_usdt: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class EquityCurve(Base):
    __tablename__ = "equity_curve"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="binance")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    equity_usdt: Mapped[float] = mapped_column(Float, nullable=False)


class CapitalFlow(Base):
    __tablename__ = "capital_flows"
    __table_args__ = (UniqueConstraint("external_id", name="uq_capital_flows_external_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="binance")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    amount_usdt: Mapped[float] = mapped_column(Float, nullable=False)
    flow_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detected_by: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    # §7.5: external_id construction is "<venue>:<flow_type>:<id>" to guarantee
    # disambiguation across venues and idempotent re-ingestion.
    external_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Legacy v1.3 column shim — predecessor of `flow_type`. Written
    # alongside `flow_type` on new rows so a legacy-shaped read (e.g.
    # the legacy bot still running in parallel) can interpret them.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="")


class ScanResult(Base):
    __tablename__ = "scan_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    exchange: Mapped[str] = mapped_column(String(16), nullable=False, index=True, default="binance")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    candidates_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candidates_passing: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top_symbols: Mapped[str] = mapped_column(Text, nullable=False, default="")
