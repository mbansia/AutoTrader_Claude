from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Position(Base):
    __tablename__ = 'positions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    spot_symbol: Mapped[str] = mapped_column(String(32))
    perp_symbol: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[float] = mapped_column(Float)
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    status: Mapped[str] = mapped_column(String(16), default='open', index=True)
    entry_funding_rate: Mapped[float] = mapped_column(Float, default=0.0)
    last_funding_rate: Mapped[float] = mapped_column(Float, default=0.0)
    spot_entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    perp_entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    funding_interval_hours: Mapped[float] = mapped_column(Float, default=8.0)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Trade(Base):
    __tablename__ = 'trades'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    venue: Mapped[str] = mapped_column(String(16))
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class EquityCurve(Base):
    __tablename__ = 'equity_curve'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    equity_usdt: Mapped[float] = mapped_column(Float)


class RejectedCandidate(Base):
    __tablename__ = 'rejected_candidates'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    funding_rate: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BotEvent(Base):
    __tablename__ = 'bot_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    level: Mapped[str] = mapped_column(String(16), default='INFO')
    message: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    requires_action: Mapped[bool] = mapped_column(Boolean, default=False)


class RuntimeState(Base):
    __tablename__ = 'runtime_state'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StrategyConfig(Base):
    __tablename__ = 'strategy_config'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Funding-rate thresholds are stored as annualized decimals (APR).
    # 0.20 = 20% APR. The bot annualizes each candidate's period rate using
    # its funding interval (4h or 8h on Binance) before comparing.
    entry_funding_threshold: Mapped[float] = mapped_column(Float, default=0.20)
    exit_funding_threshold: Mapped[float] = mapped_column(Float, default=0.05)
    max_hold_hours: Mapped[int] = mapped_column(Integer, default=72)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=1)
    max_trades_per_day: Mapped[int] = mapped_column(Integer, default=8)
    max_position_notional: Mapped[float] = mapped_column(Float, default=10.0)
    min_symbol_notional: Mapped[float] = mapped_column(Float, default=5.0)
    min_24h_quote_volume: Mapped[float] = mapped_column(Float, default=100000.0)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=-0.02)
    paper_slippage_bps: Mapped[float] = mapped_column(Float, default=5.0)
    paper_fee_bps: Mapped[float] = mapped_column(Float, default=4.0)
    loop_seconds: Mapped[int] = mapped_column(Integer, default=30)
    paper_starting_equity: Mapped[float] = mapped_column(Float, default=1000.0)
    entry_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    exit_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BalanceSnapshot(Base):
    __tablename__ = 'balance_snapshots'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    spot_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    futures_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    total_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16), default='live')


class CapitalFlow(Base):
    __tablename__ = 'capital_flows'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    amount_usdt: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(16), default='deposit')
    detected_by: Mapped[str] = mapped_column(String(16), default='auto')
    note: Mapped[str] = mapped_column(Text, default='')


class ScanResult(Base):
    __tablename__ = 'scan_results'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    candidates_total: Mapped[int] = mapped_column(Integer, default=0)
    candidates_passing: Mapped[int] = mapped_column(Integer, default=0)
    top_candidates: Mapped[str] = mapped_column(Text, default='[]')
    action: Mapped[str] = mapped_column(String(64), default='')
    note: Mapped[str] = mapped_column(Text, default='')
