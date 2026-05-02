from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


# Bot trading-mode tag attached to every per-row record so paper and live data
# never mix in the dashboard. Values: 'paper' | 'live'.
MODE_PAPER = 'paper'
MODE_LIVE = 'live'
ALL_MODES = (MODE_PAPER, MODE_LIVE)


class Position(Base):
    __tablename__ = 'positions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
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
    # Funding payments collected on this position so far. Paper mode accrues
    # synthetically each cycle; live mode reads off the futures wallet (we don't
    # attribute per-position in live for v1, so this stays at 0 there).
    funding_income_accrued: Mapped[float] = mapped_column(Float, default=0.0)
    last_funding_accrual_ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Trade(Base):
    __tablename__ = 'trades'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
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
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    equity_usdt: Mapped[float] = mapped_column(Float)


class RejectedCandidate(Base):
    __tablename__ = 'rejected_candidates'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    funding_rate: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BotEvent(Base):
    __tablename__ = 'bot_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    level: Mapped[str] = mapped_column(String(16), default='INFO')
    message: Mapped[str] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    requires_action: Mapped[bool] = mapped_column(Boolean, default=False)


class ModeState(Base):
    """Per-mode kill-switch / maintenance state. Two rows: 'paper' and 'live'."""
    __tablename__ = 'mode_state'
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    entry_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    exit_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# Legacy single-row "active mode" table. Retained so existing DBs don't break,
# but the bot no longer reads paper_mode for routing — both modes run.
class RuntimeState(Base):
    __tablename__ = 'runtime_state'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    paper_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    maintenance_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StrategyConfig(Base):
    __tablename__ = 'strategy_config'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_funding_threshold: Mapped[float] = mapped_column(Float, default=0.20)
    exit_funding_threshold: Mapped[float] = mapped_column(Float, default=0.05)
    max_hold_hours: Mapped[int] = mapped_column(Integer, default=72)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=1)
    max_trades_per_day: Mapped[int] = mapped_column(Integer, default=8)
    # Position sizing as a fraction of current portfolio equity (0.10 = 10%).
    # The legacy USDT columns remain in the table for back-compat but are unused.
    min_position_pct: Mapped[float] = mapped_column(Float, default=0.005)
    max_position_pct: Mapped[float] = mapped_column(Float, default=0.10)
    max_position_notional: Mapped[float] = mapped_column(Float, default=10.0)  # deprecated
    min_symbol_notional: Mapped[float] = mapped_column(Float, default=5.0)     # deprecated
    min_24h_quote_volume: Mapped[float] = mapped_column(Float, default=100000.0)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=-0.02)
    paper_slippage_bps: Mapped[float] = mapped_column(Float, default=5.0)
    paper_fee_bps: Mapped[float] = mapped_column(Float, default=4.0)
    loop_seconds: Mapped[int] = mapped_column(Integer, default=30)
    paper_starting_equity: Mapped[float] = mapped_column(Float, default=1000.0)
    entry_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    exit_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_entry_basis_bps: Mapped[float] = mapped_column(Float, default=20.0)
    max_exit_basis_bps: Mapped[float] = mapped_column(Float, default=5.0)
    enforce_hedge_check: Mapped[bool] = mapped_column(Boolean, default=True)
    delisting_check: Mapped[bool] = mapped_column(Boolean, default=True)
    # Earn / Binance Simple Earn Flexible USDT sweep.
    earn_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    earn_idle_threshold_usdt: Mapped[float] = mapped_column(Float, default=1.0)
    earn_paper_apr: Mapped[float] = mapped_column(Float, default=0.05)
    # Live-only: auto-move USDT spot↔futures so the perp leg always has margin
    # and idle USDT ends up back in spot for Earn sweeping.
    auto_transfer_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Continuous spot ↔ futures rebalance: keep both wallets' free balances near
    # equal each cycle. Threshold is the minimum imbalance (USDT) before we move.
    auto_rebalance_threshold: Mapped[float] = mapped_column(Float, default=1.0)
    # Subscribe base assets (SOL, ETH, etc.) to Binance Simple Earn Flexible after
    # the spot buy fills. Default off — opt-in because not every asset has a
    # flexible product and redeem-on-close is one extra failure mode.
    earn_subscribe_spot_assets: Mapped[bool] = mapped_column(Boolean, default=False)
    # Leverage for the perp leg. 1x is the only safe choice for a hedge — keeps
    # the perp's used margin equal to its notional, matching the spot leg.
    perp_leverage: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BalanceSnapshot(Base):
    __tablename__ = 'balance_snapshots'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    spot_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    futures_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    total_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    # 'paper' or 'live' — predates the formal `mode` convention; kept compatible.
    source: Mapped[str] = mapped_column(String(16), default=MODE_PAPER, index=True)


class CapitalFlow(Base):
    __tablename__ = 'capital_flows'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    amount_usdt: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(16), default='deposit')
    detected_by: Mapped[str] = mapped_column(String(16), default='auto')
    note: Mapped[str] = mapped_column(Text, default='')


class ScanResult(Base):
    __tablename__ = 'scan_results'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    candidates_total: Mapped[int] = mapped_column(Integer, default=0)
    candidates_passing: Mapped[int] = mapped_column(Integer, default=0)
    top_candidates: Mapped[str] = mapped_column(Text, default='[]')
    action: Mapped[str] = mapped_column(String(64), default='')
    note: Mapped[str] = mapped_column(Text, default='')


class EarnState(Base):
    """Per-mode tracking of USDT swept into Binance Simple Earn (or paper-simulated)."""
    __tablename__ = 'earn_state'
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    deployed_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    cumulative_yield_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    last_accrual_ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_error: Mapped[str] = mapped_column(Text, default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
