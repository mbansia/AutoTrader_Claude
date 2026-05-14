"""Core: pure-logic domain (no I/O). Implements docs/SYSTEM.md §0 + §3.1 math."""

from .lifecycle import (
    IllegalTransition,
    LifecycleEvent,
    initial_status,
    is_currently_exposed,
    transition,
)
from .math import (
    apy_compound,
    basis_bps,
    basis_dislocation_triggered,
    basis_rt_signed_bps,
    evaluate_gate,
    fees_rt_bps,
    forward_net_apy_for_exit,
    periods_per_year,
    unrealized_pnl_fraction,
    worst_adverse_swing_bps,
)
from .sizing import (
    size_buy_spot_price_dependent,
    size_entry,
    size_perp_flat_cap,
    size_sell_spot_flat_cap,
    snapshots_co_temporal,
)
from .types import (
    OPEN_STATUSES,
    BookLevel,
    BookSnapshot,
    BotEvent,
    ExchangeId,
    FeeInfo,
    FillResult,
    FundingInfo,
    GateInputs,
    GateResult,
    MergedConfig,
    Mode,
    Position,
    PositionStatus,
    RejectedCandidate,
    ScanCandidate,
    Side,
    SizingResult,
    Trade,
    VenueLeg,
    WalletBalance,
    utcnow,
)
