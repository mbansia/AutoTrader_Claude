"""SQLAlchemy models — the canonical schema for the bot's local store.

Every per-row table carries a :data:`mode` tag (``'paper'`` or
``'live'``) so the same DB cleanly serves both views. Aggregate tables
(:class:`StrategyConfig`, :class:`RuntimeState`) are single-row
singletons; :class:`ModeState` and :class:`EarnState` are keyed by mode.

Schema versioning is in-place via :func:`app.db.run_schema_migrations`
which adds new columns with defaults. We never destructively alter
existing columns — when semantics change (e.g. funding thresholds going
from per-period to APY) we add a one-time data migration in
:func:`app.bot.get_strategy_config`.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


# Bot trading-mode tag attached to every per-row record so paper and live data
# never mix in the dashboard. Values: 'paper' | 'live'.
MODE_PAPER = 'paper'
MODE_LIVE = 'live'
ALL_MODES = (MODE_PAPER, MODE_LIVE)


# Venue tag — which exchange / broker the row lives on. The system treats the
# entire portfolio as a single pool of capital distributed across venues. Today
# Binance is the only live venue; KuCoin and Interactive Brokers will be added
# without requiring schema changes by extending this list and the gateway
# factory in app.exchange. Every Position / Trade carries a venue so the UI can
# break down equity, PnL, and exposure by venue without ambiguity.
VENUE_BINANCE = 'binance'
VENUE_KUCOIN = 'kucoin'
VENUE_IBKR = 'ibkr'
ALL_VENUES = (VENUE_BINANCE, VENUE_KUCOIN, VENUE_IBKR)


# Trade-type taxonomy. Each bot-originated position carries one of these
# tags so we can compute per-strategy PnL, run different buffer / sizing
# rules per type, and (later) route entries through the right multi-venue
# orchestrator. Tags are stored as plain strings so we can introduce new
# types without a schema migration; see TRADE_TYPE_LABELS for the
# human-readable display strings.
TRADE_TYPE_BINANCE_FUNDING_ARB = 'binance_same_venue_funding_arb'   # long spot + short perp on Binance only
TRADE_TYPE_KUCOIN_FUNDING_ARB = 'kucoin_same_venue_funding_arb'     # long spot + short perp on KuCoin only
TRADE_TYPE_BINANCE_KUCOIN_ARB = 'binance_kucoin_cross_funding_arb'  # spot one side, perp the other (Phase 2)
TRADE_TYPE_ONCHAIN_BINANCE_ARB = 'onchain_binance_funding_arb'      # DEX perp + CEX spot (Phase 3)
TRADE_TYPE_IBKR_BINANCE_ARB = 'ibkr_binance_funding_arb'            # IBKR equity / option leg + Binance perp (future)
TRADE_TYPE_IBKR_KUCOIN_ARB = 'ibkr_kucoin_funding_arb'              # IBKR + KuCoin equivalent (future)
TRADE_TYPE_LABELS: dict[str, str] = {
    TRADE_TYPE_BINANCE_FUNDING_ARB: 'binance · same-venue funding arb',
    TRADE_TYPE_KUCOIN_FUNDING_ARB: 'kucoin · same-venue funding arb',
    TRADE_TYPE_BINANCE_KUCOIN_ARB: 'binance ↔ kucoin · cross-venue arb',
    TRADE_TYPE_ONCHAIN_BINANCE_ARB: 'onchain ↔ binance · cross-venue arb',
    TRADE_TYPE_IBKR_BINANCE_ARB: 'ibkr ↔ binance · cross-asset arb',
    TRADE_TYPE_IBKR_KUCOIN_ARB: 'ibkr ↔ kucoin · cross-asset arb',
}


def venue_to_trade_type(venue_id: str) -> str:
    """Default mapping for the same-venue funding arb the bot runs today.
    Used when opening a new position; later, multi-venue orchestrators
    will pick the trade type explicitly when they create the position."""
    return {
        VENUE_BINANCE: TRADE_TYPE_BINANCE_FUNDING_ARB,
        VENUE_KUCOIN: TRADE_TYPE_KUCOIN_FUNDING_ARB,
    }.get(venue_id, TRADE_TYPE_BINANCE_FUNDING_ARB)


class Position(Base):
    __tablename__ = 'positions'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    # Which exchange / broker the position lives on. Today every position is
    # ``VENUE_BINANCE``; KuCoin and Interactive Brokers will populate this
    # column once their gateways land. The dashboard breaks down equity, PnL
    # and exposure across venues using this tag.
    exchange: Mapped[str] = mapped_column(String(16), default=VENUE_BINANCE, index=True)
    # Trade-type tag — see TRADE_TYPE_* constants above. Same-venue funding
    # arbs (Binance/KuCoin) are the only types the bot opens today; cross-
    # venue and onchain entries land in this column once their orchestrators
    # are wired. Indexed because the dashboard groups by it.
    trade_type: Mapped[str] = mapped_column(String(48), default=TRADE_TYPE_BINANCE_FUNDING_ARB, index=True)
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
    # Last close-attempt error message for this position, if a close is failing
    # repeatedly. Cleared once the close goes through. Surfaced on the UI so the
    # user can see at a glance why a position is stuck.
    last_close_error: Mapped[str] = mapped_column(Text, default='')
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Trade(Base):
    __tablename__ = 'trades'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    # Cross-venue tag — which broker / exchange the fill landed on. Together
    # with ``venue`` (which is the leg: 'spot' / 'futures') this forms the
    # full coordinate of the fill. ``exchange`` is the dimension we'll add
    # KuCoin / Interactive Brokers to.
    exchange: Mapped[str] = mapped_column(String(16), default=VENUE_BINANCE, index=True)
    # Trade-type tag mirrored from the parent Position so per-strategy
    # PnL / fee analysis can group trade rows directly without joining.
    trade_type: Mapped[str] = mapped_column(String(48), default=TRADE_TYPE_BINANCE_FUNDING_ARB, index=True)
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    venue: Mapped[str] = mapped_column(String(16))  # 'spot' | 'futures' — the leg, kept for backwards compat
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[float] = mapped_column(Float)
    price: Mapped[float] = mapped_column(Float)
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class EquityCurve(Base):
    __tablename__ = 'equity_curve'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    # Per-venue equity curve so dashboard can chart venues separately and
    # aggregate them as needed. Defaults to ``binance`` for back-compat with
    # rows written before Phase 1.
    exchange: Mapped[str] = mapped_column(String(16), default=VENUE_BINANCE, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    equity_usdt: Mapped[float] = mapped_column(Float)


class RejectedCandidate(Base):
    __tablename__ = 'rejected_candidates'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    exchange: Mapped[str] = mapped_column(String(16), default=VENUE_BINANCE, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    reason: Mapped[str] = mapped_column(Text)
    funding_rate: Mapped[float] = mapped_column(Float, default=0.0)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BotEvent(Base):
    __tablename__ = 'bot_events'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    # 'binance' | 'kucoin' | 'system' (cross-venue cycle events). Lets the
    # logs page filter by venue and the export-md split events per venue.
    exchange: Mapped[str] = mapped_column(String(16), default=VENUE_BINANCE, index=True)
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
    # Order-book depth liquidity filter. Reject any candidate where the smaller
    # of (spot ask depth, perp bid depth) within ±depth_band_bps of mid is below
    # this threshold. 0 disables the filter.
    min_order_book_depth_usdt: Mapped[float] = mapped_column(Float, default=500.0)
    depth_band_bps: Mapped[float] = mapped_column(Float, default=10.0)
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
    # Liquidation buffer: % of open perp notional kept as free margin in the
    # futures wallet between cycles. Cross + 1x means used_margin == notional;
    # this buffer is what absorbs adverse mark-price moves before maintenance
    # margin triggers. 20% covers roughly a -20% move on the short side, which
    # is generous for the typical mid/large-cap crypto we trade. Bump higher
    # for memes/low-liquidity tokens. Cross margin shares this buffer across
    # all open perps on the same venue, so the safety scales naturally.
    futures_buffer_pct: Mapped[float] = mapped_column(Float, default=0.20)
    # Max perp leverage the bot configures on every USDM perp before entry.
    # 1x is the only safe choice for a delta-neutral arb (perp margin ==
    # spot notional). Bump only if you understand the liquidation math.
    max_perp_leverage: Mapped[int] = mapped_column(Integer, default=1)
    # Max share of total Binance equity that may be auto-converted to
    # BFUSD (Binance's yield-bearing margin asset under Portfolio Margin).
    # The cap is a safety throttle on rollout: BFUSD redeems are usually
    # instant but can queue under stress, so we keep some plain USDT for
    # immediate-deploy. Default 0.20 (20% of equity); bump to 0.80 once
    # BFUSD redemption behaviour has been observed under your trading load.
    binance_max_bfusd_pct: Mapped[float] = mapped_column(Float, default=0.20)
    # Subscribe base assets (SOL, ETH, etc.) to Binance Simple Earn Flexible after
    # the spot buy fills. Default off — opt-in because not every asset has a
    # flexible product and redeem-on-close is one extra failure mode.
    earn_subscribe_spot_assets: Mapped[bool] = mapped_column(Boolean, default=False)
    # Leverage for the perp leg. 1x is the only safe choice for a hedge — keeps
    # the perp's used margin equal to its notional, matching the spot leg.
    perp_leverage: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BalanceSnapshot(Base):
    """Per-cycle, per-(mode, venue) snapshot of the wallet balances. The
    dashboard reads the latest row per venue and sums to get the live total.
    Multi-venue aware: previously only carried ``source=mode`` so two venues
    in the same mode would race each other on every cycle."""
    __tablename__ = 'balance_snapshots'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    spot_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    futures_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    total_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    # 'paper' or 'live' — predates the formal `mode` convention; kept compatible.
    source: Mapped[str] = mapped_column(String(16), default=MODE_PAPER, index=True)
    # Which venue this snapshot is for. Default 'binance' for back-compat.
    exchange: Mapped[str] = mapped_column(String(16), default=VENUE_BINANCE, index=True)


class CapitalFlow(Base):
    __tablename__ = 'capital_flows'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    # Which venue the flow happened on. Manual flows can be attributed to a
    # specific venue (e.g. user deposited $20 into the KuCoin sub-account)
    # so net-injected-capital aggregates correctly across venues.
    exchange: Mapped[str] = mapped_column(String(16), default=VENUE_BINANCE, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    amount_usdt: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(16), default='deposit')
    detected_by: Mapped[str] = mapped_column(String(16), default='auto')
    note: Mapped[str] = mapped_column(Text, default='')
    # Stable per-venue id for the underlying transaction. Set for rows
    # ingested from venue APIs so re-running the ingest is idempotent;
    # blank for manual rows (which have no venue-side identifier).
    external_id: Mapped[str] = mapped_column(String(128), default='', index=True)


class ScanResult(Base):
    __tablename__ = 'scan_results'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default=MODE_PAPER, index=True)
    # Which venue this scan covers. The Logs tab groups scans by venue so the
    # user can see whether KuCoin's funding scan is actually running.
    exchange: Mapped[str] = mapped_column(String(16), default=VENUE_BINANCE, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    candidates_total: Mapped[int] = mapped_column(Integer, default=0)
    candidates_passing: Mapped[int] = mapped_column(Integer, default=0)
    top_candidates: Mapped[str] = mapped_column(Text, default='[]')
    action: Mapped[str] = mapped_column(String(64), default='')
    note: Mapped[str] = mapped_column(Text, default='')


class EarnState(Base):
    """Per-(mode, venue) tracking of USDT in earn surfaces (Binance Simple
    Earn flexible, KuCoin funding-wallet auto-lend, paper-simulated). The
    composite primary key is critical: a single shared row across venues
    causes each gateway's refresh to overwrite the previous, producing
    nonsense aggregate balances and runaway cumulative_yield deltas."""
    __tablename__ = 'earn_state'
    mode: Mapped[str] = mapped_column(String(8), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(16), primary_key=True, default=VENUE_BINANCE)
    deployed_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    cumulative_yield_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    last_accrual_ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    last_error: Mapped[str] = mapped_column(Text, default='')
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
