"""Cycle orchestrator. One pass = Phase A safety → Phase B exits → Phase C
entries → post-cycle bookkeeping. Mirrors docs/SYSTEM.md §3.1 SOP.

The orchestrator is venue-agnostic: it takes a `Gateway` and a `MergedConfig`
and runs the phases. Each phase function is its own callable so it can be
swapped for a stub in tests.

Mode handling: paper mode runs the read-only probes (Phase A's market-health,
hedge-integrity checks) AGAINST the venue, but order placement is synthetic
via the in-memory gateway. Live mode hits real venues for both reads + orders.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.lifecycle import LifecycleEvent, initial_status, transition
from core.math import (
    basis_bps,
    basis_dislocation_triggered,
    evaluate_gate,
    forward_net_apy_for_exit,
    unrealized_pnl_fraction,
)
from core.sizing import size_entry, snapshots_co_temporal
from core.types import (
    BookSnapshot,
    GateInputs,
    MergedConfig,
    Mode,
    PositionStatus,
    ScanCandidate,
)
from gateways.base import Gateway
from state import models as m
from state import repository as repo

from .executor import EntryOutcome, parallel_market_fok_entry
from .idempotency import client_order_id, make_cycle_id


log = logging.getLogger(__name__)


@dataclass(slots=True)
class CycleResult:
    cycle_id: str
    mode: Mode
    exchange: str
    actions: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    error: str | None = None


def run_cycle(
    *,
    session: Session,
    gateway: Gateway,
    mode: Mode,
    config: MergedConfig,
    trade_type: str = "binance_same_venue_funding_arb",
) -> CycleResult:
    """One end-to-end cycle. Returns the outcome for diagnostics + tests."""
    cycle_id = make_cycle_id()
    res = CycleResult(cycle_id=cycle_id, mode=mode, exchange=gateway.exchange_id)

    try:
        # Phase A — safety (omitted in paper mode for hedge-integrity since
        # `list_open_perp_positions` may not be meaningful with synthetic state;
        # the test harness exercises this with explicit fixtures).
        _phase_a_safety(session, gateway, mode, config, trade_type, res)

        # Maintenance-mode short-circuit: close all, block new entries.
        mode_state = repo.get_or_create_mode_state(session, mode)
        if mode_state.maintenance_mode:
            _close_all_for_maintenance(session, gateway, mode, trade_type, cycle_id, res)
            res.actions.append("maintenance_close_run")
            return res

        # Phase B — exits
        _phase_b_exits(session, gateway, mode, config, trade_type, res)

        # Phase C — entries
        if mode_state.entry_enabled:
            _phase_c_entries(session, gateway, mode, config, trade_type, cycle_id, res)

    except Exception as exc:
        # §3.1 SOP "Crash handling": outer exception caught, logged, loop continues.
        # We persist the ERROR so the cycle_error_rate_high anomaly can fire.
        log.exception("cycle_error")
        res.error = str(exc)
        repo.log_event(
            session,
            mode=mode,
            exchange=gateway.exchange_id,
            level="ERROR",
            message=f"Loop iteration error ({mode}): {exc!r}",
        )

    return res


# ─── Phase A — safety ───────────────────────────────────────────────────────

def _phase_a_safety(
    session: Session,
    gateway: Gateway,
    mode: Mode,
    config: MergedConfig,
    trade_type: str,
    res: CycleResult,
) -> None:
    """Three sub-passes per §3.1 SOP v1.4:
      1. stale reconciliation for naked rows
      2. hedge-integrity for open rows
      3. phantom-leg recovery (naked → hedged or closed)
    """
    if mode != "live":
        # Paper-mode Phase A is intentionally gated off in this skeleton: the
        # paper gateway doesn't yet ledger open-perp positions or stale-balance
        # disappearances, so the safety checks would mis-fire on synthetic
        # state. The live-mode parity work (§17 Stage 5) is where paper-mode
        # Phase A becomes meaningful. Test fixtures exercise the live-mode
        # paths directly with mode="live" + explicit gateway state.
        return

    exposed = repo.positions_currently_exposed(session, mode, exchange=gateway.exchange_id)

    # 1. stale reconciliation — for naked rows, if the underlying leg has
    # vanished from the venue, mark closed.
    for pos in exposed:
        if pos.status not in {
            PositionStatus.NAKED_SPOT.value,
            PositionStatus.NAKED_PERP.value,
        }:
            continue
        if _is_underlying_gone(gateway, pos):
            _mark_closed(
                session, pos, mode, "stale_reconciled",
                event=LifecycleEvent.STALE_RECONCILED,
            )
            res.actions.append(f"stale_reconciled:{pos.symbol}")

    # 2. hedge-integrity for open rows.
    if config.hedge_integrity_check:
        for pos in exposed:
            if pos.status == PositionStatus.OPEN.value:
                _check_hedge_integrity(session, gateway, pos, mode, res)

    # 3. phantom-leg recovery — naked rows that didn't get reconciled go
    # through the hedge-or-close path.
    refreshed = repo.positions_currently_exposed(
        session, mode, exchange=gateway.exchange_id
    )
    for pos in refreshed:
        if pos.status == PositionStatus.NAKED_SPOT.value:
            _recover_naked_spot(session, gateway, pos, mode, config, trade_type, res)
        elif pos.status == PositionStatus.NAKED_PERP.value:
            _recover_naked_perp(session, gateway, pos, mode, config, trade_type, res)


def _is_underlying_gone(gateway: Gateway, pos: m.Position) -> bool:
    """Stale reconciliation predicate (§3.1 Phase A ①, naked-direction-symmetric)."""
    eps = max(gateway.lot_step(pos.spot_symbol), gateway.lot_step(pos.perp_symbol)) * 0.5
    if pos.status == PositionStatus.NAKED_SPOT.value:
        bal = gateway.fetch_balance(pos.symbol)["spot"]
        return (bal.free + bal.total) < (pos.quantity - eps)
    if pos.status == PositionStatus.NAKED_PERP.value:
        perps = {sym: q for sym, q in gateway.list_open_perp_positions()}
        return abs(perps.get(pos.perp_symbol, 0.0)) < (pos.quantity - eps)
    return False


def _recover_naked_spot(
    session: Session,
    gateway: Gateway,
    pos: m.Position,
    mode: Mode,
    config: MergedConfig,
    trade_type: str,
    res: CycleResult,
) -> None:
    """L21 (direction-neutral): try hedge first; fall back to flat-close."""
    perp_book = gateway.snapshot_book(pos.perp_symbol)
    funding = gateway.fetch_predicted_funding(pos.perp_symbol)
    fees = gateway.fetch_fees(pos.spot_symbol, pos.perp_symbol)
    live_perp = perp_book.mid_price
    bal = gateway.fetch_balance(pos.symbol)["spot"]
    qty = min(bal.free + bal.total, pos.quantity)
    if qty <= 0.0:
        _mark_closed(session, pos, mode, "naked_spot_zero_balance")
        return

    # Forward-profit gate at zero basis (best-case for hedging an existing spot).
    fwd = forward_net_apy_for_exit(
        live_funding_rate=funding.predicted_rate,
        funding_interval_hours=funding.interval_hours,
        live_basis_bps_value=0.0,
        spot_fee_bps=fees.spot_fee_bps,
        perp_fee_bps=fees.perp_fee_bps,
        exit_basis_buffer_multiple=config.exit_basis_buffer_multiple,
    )
    if fwd >= config.entry_min_net_apy:
        # Hedge: open the missing perp short.
        coid = f"hedge-{pos.id}-perp"
        fill = gateway.place_market_fok(pos.perp_symbol, "futures", "sell", qty, coid)
        if fill.accepted:
            _mark_status(session, pos, LifecycleEvent.HEDGE_RECOVERED)
            pos.perp_entry_price = fill.avg_price
            pos.entry_funding_rate = funding.predicted_rate
            pos.last_funding_rate = funding.predicted_rate
            pos.funding_interval_hours = funding.interval_hours
            session.flush()
            repo.insert_trade(
                session,
                m.Trade(
                    mode=mode,
                    exchange=gateway.exchange_id,
                    trade_type=trade_type,
                    position_id=pos.id,
                    symbol=pos.perp_symbol,
                    venue="futures",
                    side="sell",
                    quantity=fill.qty,
                    price=fill.avg_price,
                    fee=fill.fee_quote,
                ),
            )
            res.actions.append(f"phantom_spot_rescued:{pos.symbol}")
            return

    # Fall back to sell-back via market+FOK.
    coid = f"close-{pos.id}-spot"
    fill = gateway.place_market_fok(pos.spot_symbol, "spot", "sell", qty, coid)
    if fill.accepted:
        _mark_closed(session, pos, mode, "")
        repo.insert_trade(
            session,
            m.Trade(
                mode=mode,
                exchange=gateway.exchange_id,
                trade_type=trade_type,
                position_id=pos.id,
                symbol=pos.spot_symbol,
                venue="spot",
                side="sell",
                quantity=fill.qty,
                price=fill.avg_price,
                fee=fill.fee_quote,
            ),
        )
        res.actions.append(f"phantom_spot_closed:{pos.symbol}")
        return

    pos.last_close_error = f"recover_failed:{fill.error}"
    session.flush()
    res.actions.append(f"phantom_spot_recovery_failed:{pos.symbol}")


def _recover_naked_perp(
    session: Session,
    gateway: Gateway,
    pos: m.Position,
    mode: Mode,
    config: MergedConfig,
    trade_type: str,
    res: CycleResult,
) -> None:
    """Symmetric to _recover_naked_spot. Try buying matching spot to hedge;
    fall back to buying back the perp short."""
    spot_book = gateway.snapshot_book(pos.spot_symbol)
    funding = gateway.fetch_predicted_funding(pos.perp_symbol)
    fees = gateway.fetch_fees(pos.spot_symbol, pos.perp_symbol)
    perps = {sym: q for sym, q in gateway.list_open_perp_positions()}
    qty = min(abs(perps.get(pos.perp_symbol, 0.0)), pos.quantity)
    if qty <= 0.0:
        _mark_closed(session, pos, mode, "naked_perp_zero_position")
        return

    fwd = forward_net_apy_for_exit(
        live_funding_rate=funding.predicted_rate,
        funding_interval_hours=funding.interval_hours,
        live_basis_bps_value=0.0,
        spot_fee_bps=fees.spot_fee_bps,
        perp_fee_bps=fees.perp_fee_bps,
        exit_basis_buffer_multiple=config.exit_basis_buffer_multiple,
    )
    if fwd >= config.entry_min_net_apy:
        # Hedge by buying the missing spot leg.
        coid = f"hedge-{pos.id}-spot"
        fill = gateway.place_market_fok(pos.spot_symbol, "spot", "buy", qty, coid)
        if fill.accepted:
            _mark_status(session, pos, LifecycleEvent.HEDGE_RECOVERED)
            pos.spot_entry_price = fill.avg_price
            session.flush()
            repo.insert_trade(
                session,
                m.Trade(
                    mode=mode,
                    exchange=gateway.exchange_id,
                    trade_type=trade_type,
                    position_id=pos.id,
                    symbol=pos.spot_symbol,
                    venue="spot",
                    side="buy",
                    quantity=fill.qty,
                    price=fill.avg_price,
                    fee=fill.fee_quote,
                ),
            )
            res.actions.append(f"phantom_perp_rescued:{pos.symbol}")
            return

    # Fall back to buying back the perp short.
    coid = f"close-{pos.id}-perp"
    fill = gateway.place_market_fok(pos.perp_symbol, "futures", "buy", qty, coid)
    if fill.accepted:
        _mark_closed(session, pos, mode, "")
        repo.insert_trade(
            session,
            m.Trade(
                mode=mode,
                exchange=gateway.exchange_id,
                trade_type=trade_type,
                position_id=pos.id,
                symbol=pos.perp_symbol,
                venue="futures",
                side="buy",
                quantity=fill.qty,
                price=fill.avg_price,
                fee=fill.fee_quote,
            ),
        )
        res.actions.append(f"phantom_perp_closed:{pos.symbol}")
        return

    pos.last_close_error = f"recover_failed:{fill.error}"
    session.flush()
    res.actions.append(f"phantom_perp_recovery_failed:{pos.symbol}")


def _check_hedge_integrity(
    session: Session, gateway: Gateway, pos: m.Position, mode: Mode, res: CycleResult
) -> None:
    base = pos.symbol
    balances = gateway.fetch_balance(base)
    # ε in BASE units. 0.5 of the lot step is conservative — any difference
    # below half a lot step is rounding noise, not a missing leg.
    eps = max(gateway.lot_step(pos.spot_symbol), gateway.lot_step(pos.perp_symbol)) * 0.5
    spot_balance_total = balances["spot"].free + balances["spot"].total
    spot_present = spot_balance_total >= (pos.quantity - eps)

    perps = {sym: q for sym, q in gateway.list_open_perp_positions()}
    perp_qty = abs(perps.get(pos.perp_symbol, 0.0))
    perp_present = perp_qty >= (pos.quantity - eps)

    if pos.status == PositionStatus.OPEN.value:
        if not spot_present and not perp_present:
            _mark_closed(session, pos, mode, "hedge_integrity:both_legs_missing")
            res.actions.append(f"closed:{pos.symbol}:hedge_both_missing")
        elif not spot_present:
            _mark_status(session, pos, LifecycleEvent.DISCOVER_ORPHAN_PERP)
            repo.log_event(
                session,
                mode=mode,
                exchange=gateway.exchange_id,
                level="WARN",
                message=f"hedge_integrity: spot leg vanished for {pos.symbol}",
            )
            res.actions.append(f"naked_perp:{pos.symbol}")
        elif not perp_present:
            _mark_status(session, pos, LifecycleEvent.DISCOVER_ORPHAN_SPOT)
            repo.log_event(
                session,
                mode=mode,
                exchange=gateway.exchange_id,
                level="WARN",
                message=f"hedge_integrity: perp leg vanished for {pos.symbol}",
            )
            res.actions.append(f"naked_spot:{pos.symbol}")


def _close_all_for_maintenance(
    session: Session,
    gateway: Gateway,
    mode: Mode,
    trade_type: str,
    cycle_id: str,
    res: CycleResult,
) -> None:
    """Maintenance-mode: force-close every exposed position via market+FOK.
    Deferral disabled. Per §3.1 SOP v1.4.
    """
    exposed = repo.positions_currently_exposed(session, mode, exchange=gateway.exchange_id)
    for pos in exposed:
        _close_position_market_fok(session, gateway, pos, mode, cycle_id, res, "maintenance")


# ─── Phase B — exits ────────────────────────────────────────────────────────

def _phase_b_exits(
    session: Session,
    gateway: Gateway,
    mode: Mode,
    config: MergedConfig,
    trade_type: str,
    res: CycleResult,
) -> None:
    exposed = [
        p for p in repo.positions_currently_exposed(session, mode, exchange=gateway.exchange_id)
        if p.status == PositionStatus.OPEN.value
    ]
    if not exposed:
        return
    for pos in exposed:
        _evaluate_position_exit(session, gateway, pos, mode, config, res)


def _evaluate_position_exit(
    session: Session,
    gateway: Gateway,
    pos: m.Position,
    mode: Mode,
    config: MergedConfig,
    res: CycleResult,
) -> None:
    funding = gateway.fetch_predicted_funding(pos.perp_symbol)
    pos.last_funding_rate = funding.predicted_rate
    pos.funding_interval_hours = funding.interval_hours
    session.flush()

    spot_book = gateway.snapshot_book(pos.spot_symbol)
    perp_book = gateway.snapshot_book(pos.perp_symbol)
    if not snapshots_co_temporal(spot_book, perp_book):
        repo.log_event(
            session,
            mode=mode,
            exchange=gateway.exchange_id,
            level="WARN",
            message=f"snapshot_skew:{pos.symbol}",
        )
        return

    spot_mark = spot_book.mid_price
    perp_mark = perp_book.mid_price
    live_basis = basis_bps(perp_mark, spot_mark)
    entry_basis = basis_bps(pos.perp_entry_price, pos.spot_entry_price)

    fees = gateway.fetch_fees(pos.spot_symbol, pos.perp_symbol)

    # Mandatory triggers
    pnl_frac = unrealized_pnl_fraction(
        pos.quantity, pos.spot_entry_price, pos.perp_entry_price, spot_mark, perp_mark
    )
    if pnl_frac <= config.stop_loss_pct:
        _close_position_market_fok(session, gateway, pos, mode, "exit", res, "stop_loss")
        return
    if basis_dislocation_triggered(entry_basis, live_basis, config.basis_dislocation_exit_bps):
        _close_position_market_fok(session, gateway, pos, mode, "exit", res, "basis_dislocation")
        return

    # Voluntary trigger (deferrable on adverse close basis)
    fwd_apy = forward_net_apy_for_exit(
        live_funding_rate=funding.predicted_rate,
        funding_interval_hours=funding.interval_hours,
        live_basis_bps_value=live_basis,
        spot_fee_bps=fees.spot_fee_bps,
        perp_fee_bps=fees.perp_fee_bps,
        exit_basis_buffer_multiple=config.exit_basis_buffer_multiple,
    )
    if fwd_apy < config.exit_min_net_apy:
        # Deferral: signed-direction. For long-spot/short-perp, closing means
        # SELL spot + BUY perp. Adverse-close basis is "perp expensive vs spot"
        # = POSITIVE live_basis. Negative live_basis is favourable for closing
        # (perp cheap → buy back cheap). Per §3.1: defer only on adverse.
        if live_basis > config.max_exit_basis_bps:
            res.actions.append(f"defer_exit:{pos.symbol}:basis_unfavourable")
            return
        _close_position_market_fok(session, gateway, pos, mode, "exit", res, "forward_profit")


# ─── Phase C — entries ──────────────────────────────────────────────────────

def _phase_c_entries(
    session: Session,
    gateway: Gateway,
    mode: Mode,
    config: MergedConfig,
    trade_type: str,
    cycle_id: str,
    res: CycleResult,
) -> None:
    cap_open = config.max_open_positions
    exposed_count = len(repo.positions_currently_exposed(session, mode))
    if exposed_count >= cap_open:
        res.actions.append("at_capacity")
        return

    # Tier-1 scan: cheap pre-filter on every venue perp.
    candidates = _tier1_scan(gateway, config)
    if not candidates:
        res.actions.append("no_candidates")
        return

    # Skip candidates we already hold.
    held_bases = {
        p.symbol
        for p in repo.positions_currently_exposed(session, mode, exchange=gateway.exchange_id)
    }
    candidates = [c for c in candidates if c.base_asset not in held_bases]
    if not candidates:
        res.actions.append("all_candidates_already_held")
        return

    # Take the top candidate (paper mode + tests). Live ranks by approx_net_apy desc.
    candidates.sort(key=lambda c: c.approx_net_apy, reverse=True)
    candidate = candidates[0]

    # Snapshot both books co-temporally.
    spot_book = gateway.snapshot_book(candidate.spot_symbol)
    perp_book = gateway.snapshot_book(candidate.perp_symbol)
    if not snapshots_co_temporal(spot_book, perp_book):
        repo.insert_rejection(
            session,
            mode=mode,
            exchange=gateway.exchange_id,
            symbol=candidate.perp_symbol,
            reason="snapshot_skew",
            funding_rate=candidate.predicted_funding_rate,
        )
        res.rejections.append(f"{candidate.perp_symbol}:snapshot_skew")
        return

    # Closed-form sizing.
    spot_bal = gateway.fetch_balance(candidate.spot_quote_currency)["spot"]
    perp_bal = gateway.fetch_balance(candidate.quote_currency)["futures"]
    operator_cap_qty = (
        config.max_position_pct * (spot_bal.free + perp_bal.free) / spot_book.mid_price
    )
    sizing = size_entry(
        spot_book=spot_book,
        perp_book=perp_book,
        spot_wallet_quote_free=spot_bal.free,
        perp_wallet_quote_free=perp_bal.free,
        leverage=float(config.perp_leverage),
        safety=0.99,
        sub_target=config.sub_target_sizing_factor,
        operator_cap_qty=operator_cap_qty,
        spot_lot_step=gateway.lot_step(candidate.spot_symbol),
        perp_lot_step=gateway.lot_step(candidate.perp_symbol),
    )
    if sizing.target_qty <= 0.0:
        repo.insert_rejection(
            session,
            mode=mode,
            exchange=gateway.exchange_id,
            symbol=candidate.perp_symbol,
            reason=sizing.rejection_reason or "zero_target_qty",
            funding_rate=candidate.predicted_funding_rate,
        )
        res.rejections.append(f"{candidate.perp_symbol}:{sizing.rejection_reason}")
        return

    # Floor check: notional below min_position_pct of equity → reject.
    notional = sizing.target_qty * sizing.forecast_spot_avg_price
    total_equity = spot_bal.free + perp_bal.free  # paper sanity; live uses balance-snapshot
    if notional < config.min_position_pct * total_equity:
        repo.insert_rejection(
            session,
            mode=mode,
            exchange=gateway.exchange_id,
            symbol=candidate.perp_symbol,
            reason="below_min_pct_after_clamp",
            funding_rate=candidate.predicted_funding_rate,
        )
        res.rejections.append(f"{candidate.perp_symbol}:below_min_pct")
        return

    # Tier-3 profitability gate with real fees + fill basis.
    fees = gateway.fetch_fees(candidate.spot_symbol, candidate.perp_symbol)
    entry_basis = basis_bps(sizing.forecast_perp_avg_price, sizing.forecast_spot_avg_price)
    gate = evaluate_gate(
        GateInputs(
            funding_rate=candidate.predicted_funding_rate,
            funding_interval_hours=candidate.funding_interval_hours,
            entry_basis_bps=entry_basis,
            spot_fee_bps=fees.spot_fee_bps,
            perp_fee_bps=fees.perp_fee_bps,
            swap_basis_bps=0.0,
            exit_basis_buffer_multiple=config.exit_basis_buffer_multiple,
        )
    )
    if gate.net_apy < config.entry_min_net_apy:
        repo.insert_rejection(
            session,
            mode=mode,
            exchange=gateway.exchange_id,
            symbol=candidate.perp_symbol,
            reason=f"insufficient_annualized_profit (net={gate.net_apy:.4f} < {config.entry_min_net_apy:.4f})",
            funding_rate=candidate.predicted_funding_rate,
        )
        res.rejections.append(f"{candidate.perp_symbol}:tier3_reject")
        return

    # ORDER PLACEMENT — parallel market+FOK on each leg.
    spot_coid = client_order_id(cycle_id, candidate.spot_symbol, "spot", "buy")
    perp_coid = client_order_id(cycle_id, candidate.perp_symbol, "futures", "sell")
    entry = parallel_market_fok_entry(
        gateway,
        spot_symbol=candidate.spot_symbol,
        perp_symbol=candidate.perp_symbol,
        qty=sizing.target_qty,
        spot_coid=spot_coid,
        perp_coid=perp_coid,
    )

    if entry.outcome == EntryOutcome.SUCCESS:
        assert entry.spot_fill is not None
        assert entry.perp_fill is not None
        pos = m.Position(
            mode=mode,
            exchange=gateway.exchange_id,
            trade_type=trade_type,
            status=PositionStatus.OPEN.value,
            symbol=candidate.base_asset,
            spot_symbol=candidate.spot_symbol,
            perp_symbol=candidate.perp_symbol,
            quote_currency=candidate.quote_currency,
            spot_quote_currency=candidate.spot_quote_currency,
            quantity=entry.spot_fill.qty,
            entry_funding_rate=candidate.predicted_funding_rate,
            last_funding_rate=candidate.predicted_funding_rate,
            spot_entry_price=entry.spot_fill.avg_price,
            perp_entry_price=entry.perp_fill.avg_price,
            funding_interval_hours=candidate.funding_interval_hours,
        )
        repo.insert_position(session, pos)
        repo.insert_trade(
            session,
            m.Trade(
                mode=mode,
                exchange=gateway.exchange_id,
                trade_type=trade_type,
                position_id=pos.id,
                symbol=candidate.spot_symbol,
                venue="spot",
                side="buy",
                quantity=entry.spot_fill.qty,
                price=entry.spot_fill.avg_price,
                fee=entry.spot_fill.fee_quote,
            ),
        )
        repo.insert_trade(
            session,
            m.Trade(
                mode=mode,
                exchange=gateway.exchange_id,
                trade_type=trade_type,
                position_id=pos.id,
                symbol=candidate.perp_symbol,
                venue="futures",
                side="sell",
                quantity=entry.perp_fill.qty,
                price=entry.perp_fill.avg_price,
                fee=entry.perp_fill.fee_quote,
            ),
        )
        res.actions.append(f"opened:{candidate.base_asset}:qty={entry.spot_fill.qty}")
        return

    if entry.outcome == EntryOutcome.REJECTED:
        repo.insert_rejection(
            session,
            mode=mode,
            exchange=gateway.exchange_id,
            symbol=candidate.perp_symbol,
            reason=entry.reason,
            funding_rate=candidate.predicted_funding_rate,
        )
        res.rejections.append(f"{candidate.perp_symbol}:{entry.reason}")
        return

    if entry.outcome == EntryOutcome.SINGLE_LEG_ORPHAN_RECOVERED:
        repo.insert_rejection(
            session,
            mode=mode,
            exchange=gateway.exchange_id,
            symbol=candidate.perp_symbol,
            reason="single_leg_orphan",
            funding_rate=candidate.predicted_funding_rate,
        )
        res.rejections.append(f"{candidate.perp_symbol}:single_leg_orphan_rolled_back")
        return

    if entry.outcome == EntryOutcome.SINGLE_LEG_ORPHAN_NAKED:
        # Persist as the appropriate naked status; phantom-recovery picks it up.
        filled = entry.spot_fill if (entry.spot_fill and entry.spot_fill.accepted) else entry.perp_fill
        assert filled is not None
        if filled.venue_leg == "spot":
            status = initial_status(LifecycleEvent.OPEN_ONLY_SPOT_FILLED)
            spot_entry_price = filled.avg_price
            perp_entry_price = 0.0
        else:
            status = initial_status(LifecycleEvent.OPEN_ONLY_PERP_FILLED)
            spot_entry_price = 0.0
            perp_entry_price = filled.avg_price
        pos = m.Position(
            mode=mode,
            exchange=gateway.exchange_id,
            trade_type=trade_type,
            status=status.value,
            symbol=candidate.base_asset,
            spot_symbol=candidate.spot_symbol,
            perp_symbol=candidate.perp_symbol,
            quote_currency=candidate.quote_currency,
            spot_quote_currency=candidate.spot_quote_currency,
            quantity=filled.qty,
            entry_funding_rate=candidate.predicted_funding_rate,
            last_funding_rate=candidate.predicted_funding_rate,
            spot_entry_price=spot_entry_price,
            perp_entry_price=perp_entry_price,
            funding_interval_hours=candidate.funding_interval_hours,
        )
        repo.insert_position(session, pos)
        repo.log_event(
            session,
            mode=mode,
            exchange=gateway.exchange_id,
            level="WARN",
            message=f"single_leg_orphan_naked:{candidate.base_asset}:{filled.venue_leg}",
            requires_action=True,
        )
        res.actions.append(f"naked_persisted:{candidate.base_asset}:{status.value}")


def _tier1_scan(gateway: Gateway, config: MergedConfig) -> list[ScanCandidate]:
    """Tier-1 pre-filter — cheap, per-pair. Returns candidates passing the
    approximate net-APY threshold. Live mode uses the gateway's funding
    rates and a heuristic ~5 bps/leg fee assumption.

    Symbol shape is venue-specific; the gateway is responsible for spot-pair
    existence and quote-mapping (we treat the perp symbol as the source of
    truth for the candidate).
    """
    candidates: list[ScanCandidate] = []
    approx_fee_bps = 5.0
    approx_swap_basis = 0.0
    # Net APY at zero basis:
    for fr in gateway.fetch_funding_rates():
        f_w = fr.predicted_rate * 10_000.0
        rt_fees = 2.0 * (approx_fee_bps + approx_fee_bps) + (
            2.0 * (approx_fee_bps + approx_swap_basis) if approx_swap_basis > 0 else 0.0
        )
        per_w = f_w - rt_fees
        n = 24.0 * 365.0 / fr.interval_hours
        net_apy = (1.0 + per_w / 10_000.0) ** n - 1.0
        if net_apy < config.entry_min_net_apy:
            continue
        # Derive spot symbol from perp by stripping the ":settle" suffix.
        # Real gateways override this with their own market metadata.
        spot_sym = fr.symbol.split(":")[0]
        base = spot_sym.split("/")[0]
        quote = spot_sym.split("/")[1] if "/" in spot_sym else "USDT"
        candidates.append(
            ScanCandidate(
                symbol=fr.symbol,
                spot_symbol=spot_sym,
                perp_symbol=fr.symbol,
                base_asset=base,
                quote_currency=quote,
                spot_quote_currency=quote,
                predicted_funding_rate=fr.predicted_rate,
                funding_interval_hours=fr.interval_hours,
                approx_net_apy=net_apy,
            )
        )
    return candidates


# ─── helpers ────────────────────────────────────────────────────────────────

def _mark_closed(
    session: Session,
    pos: m.Position,
    mode: Mode,
    reason: str,
    *,
    event: LifecycleEvent = LifecycleEvent.FLAT_CLOSED,
) -> None:
    """Lifecycle-validated close. Passes the event through `transition()`
    so an illegal close raises and surfaces a bug rather than silently
    corrupting state. §0.4 + §16 L37.
    """
    from datetime import datetime, timezone

    current = PositionStatus(pos.status)
    next_status = transition(current, event)
    assert next_status == PositionStatus.CLOSED, f"unexpected next_status={next_status}"
    pos.status = next_status.value
    pos.closed_at = datetime.now(timezone.utc)
    pos.last_close_error = reason
    session.flush()


def _mark_status(
    session: Session, pos: m.Position, event: LifecycleEvent
) -> None:
    """Apply a state transition by event, validated against the state machine."""
    current = PositionStatus(pos.status)
    next_status = transition(current, event)
    pos.status = next_status.value
    session.flush()


def _close_position_market_fok(
    session: Session,
    gateway: Gateway,
    pos: m.Position,
    mode: Mode,
    cycle_id: str,
    res: CycleResult,
    reason: str,
) -> None:
    """Close path: parallel market+FOK in the reverse direction on each leg.
    Uses the same deterministic-coid pattern as entries.
    """
    spot_coid = client_order_id(cycle_id, pos.spot_symbol, "spot", "sell") + "-x"
    perp_coid = client_order_id(cycle_id, pos.perp_symbol, "futures", "buy") + "-x"
    spot_fill = gateway.place_market_fok(pos.spot_symbol, "spot", "sell", pos.quantity, spot_coid)
    perp_fill = gateway.place_market_fok(pos.perp_symbol, "futures", "buy", pos.quantity, perp_coid)
    if spot_fill.accepted and perp_fill.accepted:
        _mark_closed(session, pos, mode, "")
        res.actions.append(f"closed:{pos.symbol}:{reason}")
        for f in (spot_fill, perp_fill):
            repo.insert_trade(
                session,
                m.Trade(
                    mode=mode,
                    exchange=gateway.exchange_id,
                    trade_type=pos.trade_type,
                    position_id=pos.id,
                    symbol=f.symbol,
                    venue=f.venue_leg,
                    side=f.side,
                    quantity=f.qty,
                    price=f.avg_price,
                    fee=f.fee_quote,
                ),
            )
        return
    # Stuck-close: leave the row OPEN, record the failure, retry next cycle.
    err = f"close_failed:spot={spot_fill.error},perp={perp_fill.error}"
    pos.last_close_error = err
    session.flush()
    repo.log_event(
        session,
        mode=mode,
        exchange=gateway.exchange_id,
        level="ERROR",
        message=f"{err}:{pos.symbol}",
        requires_action=True,
    )
    res.actions.append(f"close_failed:{pos.symbol}")
