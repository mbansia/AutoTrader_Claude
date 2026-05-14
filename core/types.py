"""Pure-data domain types. No I/O, no SQLAlchemy, no ccxt — these are the
shapes the rest of the system passes around. Persisted in `state/models.py`,
which mirrors these.

Per docs/SYSTEM.md §0 (Definitions) + §7.1.1 (frozen column contracts).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Literal


Mode = Literal["paper", "live"]
ExchangeId = Literal["binance", "kucoin", "system"]
VenueLeg = Literal["spot", "futures"]
Side = Literal["buy", "sell"]


class PositionStatus(str, Enum):
    OPEN = "open"
    NAKED_SPOT = "naked_spot"
    NAKED_PERP = "naked_perp"
    CLOSED = "closed"


OPEN_STATUSES: tuple[PositionStatus, ...] = (
    PositionStatus.OPEN,
    PositionStatus.NAKED_SPOT,
    PositionStatus.NAKED_PERP,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class BookLevel:
    """One price/depth row from an order book snapshot. Depth is in base units."""

    price: float
    depth: float


@dataclass(slots=True)
class BookSnapshot:
    """Both sides of a book at a single instant. The closed-form sizer (§3.1
    math) needs `bids` and `asks` sorted by aggressiveness from index 0.
    `taken_at` is the snapshot timestamp; if `taken_at` drifts more than 50ms
    between spot and perp snapshots (§16 L42), the candidate is rejected
    before order placement.
    """

    symbol: str
    bids: list[BookLevel]
    asks: list[BookLevel]
    mid_price: float
    taken_at: datetime


@dataclass(slots=True)
class FundingInfo:
    symbol: str
    predicted_rate: float
    interval_hours: float
    next_funding_ts: datetime


@dataclass(slots=True)
class FeeInfo:
    spot_fee_bps: float
    perp_fee_bps: float


@dataclass(slots=True)
class WalletBalance:
    """Free / total balance for one (venue, leg, asset) triple. The cache
    must be invalidated on every mutating call (§16 L18)."""

    free: float
    total: float


@dataclass(slots=True)
class MergedConfig:
    """The view a strategy gets: per-strategy fields override the global row.
    Field set mirrors §4 active fields, post-v1.4.

    Defaults match §4 Active fields.
    """

    # Per-strategy
    entry_min_net_apy: float = 0.20
    exit_min_net_apy: float = 0.05
    exit_basis_buffer_multiple: float = 3.0
    max_exit_basis_bps: float = 5.0
    stop_loss_pct: float = -0.02
    basis_dislocation_exit_bps: float = 50.0
    min_position_pct: float = 0.005
    max_position_pct: float = 0.10
    sub_target_sizing_factor: float = 0.75
    perp_leverage: int = 1
    max_perp_leverage: int = 1
    auto_transfer_enabled: bool = True
    auto_quote_swap_enabled: bool = True
    futures_buffer_pct: float = 0.20
    depeg_guard_bps: float = 25.0

    # Global
    max_open_positions: int = 1
    max_trades_per_day: int = 8
    loop_seconds: int = 30
    paper_starting_equity: float = 1000.0
    paper_slippage_bps: float = 5.0
    paper_fee_bps: float = 4.0
    hedge_integrity_check: bool = True
    delisting_check: bool = True

    # Anomaly thresholds (§8.2 + v1.4)
    cycle_error_rate_threshold: float = 0.10
    slippage_alert_bps: float = 10.0


@dataclass(slots=True)
class ScanCandidate:
    """Output of Tier-1 scan; input to Tier-2/3."""

    symbol: str
    spot_symbol: str
    perp_symbol: str
    base_asset: str
    quote_currency: str
    spot_quote_currency: str
    predicted_funding_rate: float
    funding_interval_hours: float
    approx_net_apy: float


@dataclass(slots=True)
class SizingResult:
    """Output of core.sizing.size_candidate. The closed-form derivation
    (§3.1 math) returns the binding qty, the vwap forecast per side, and
    a rejection reason if anything zeroed out."""

    target_qty: float
    forecast_spot_avg_price: float
    forecast_perp_avg_price: float
    spot_binding_qty: float
    perp_binding_qty: float
    rejection_reason: str | None = None


@dataclass(slots=True)
class GateInputs:
    funding_rate: float
    funding_interval_hours: float
    entry_basis_bps: float
    spot_fee_bps: float
    perp_fee_bps: float
    swap_basis_bps: float = 0.0  # 0 when auto-swap not fired
    exit_basis_buffer_multiple: float = 3.0


@dataclass(slots=True)
class GateResult:
    net_apy: float
    net_per_window_bps: float
    fees_rt_bps: float
    basis_rt_signed_bps: float
    funding_per_window_bps: float


@dataclass(slots=True)
class FillResult:
    """What a gateway returns after placing one leg."""

    symbol: str
    venue_leg: VenueLeg
    side: Side
    qty: float
    avg_price: float
    fee_quote: float
    accepted: bool
    error: str | None = None
    client_order_id: str = ""


@dataclass(slots=True)
class Position:
    """In-memory shape; state/models.py persists it."""

    id: int | None
    mode: Mode
    exchange: ExchangeId
    trade_type: str
    status: PositionStatus
    symbol: str
    spot_symbol: str
    perp_symbol: str
    quote_currency: str
    spot_quote_currency: str
    quantity: float
    opened_at: datetime
    entry_funding_rate: float
    last_funding_rate: float
    spot_entry_price: float
    perp_entry_price: float
    funding_interval_hours: float
    funding_income_accrued: float = 0.0
    last_funding_accrual_ts: datetime = field(default_factory=utcnow)
    last_close_error: str = ""
    closed_at: datetime | None = None


@dataclass(slots=True)
class Trade:
    id: int | None
    mode: Mode
    exchange: ExchangeId
    trade_type: str
    position_id: int | None
    symbol: str
    venue_leg: VenueLeg
    side: Side
    quantity: float
    price: float
    fee: float
    ts: datetime = field(default_factory=utcnow)


@dataclass(slots=True)
class BotEvent:
    """An entry into the bot_events log."""

    mode: Mode
    exchange: ExchangeId
    level: Literal["INFO", "WARN", "ERROR"]
    message: str
    ts: datetime = field(default_factory=utcnow)
    requires_action: bool = False


@dataclass(slots=True)
class RejectedCandidate:
    mode: Mode
    exchange: ExchangeId
    symbol: str
    reason: str
    funding_rate: float
    ts: datetime = field(default_factory=utcnow)
