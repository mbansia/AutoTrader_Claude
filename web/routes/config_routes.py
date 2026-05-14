"""Configuration form. §4 + §5.3 per-strategy split."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from state import models as m
from state import session_scope
from state.repository import get_or_create_strategy_config, get_or_seed_per_strategy_config
from web.auth import require_basic_auth
from web.view_mode import get_view_mode


router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


# §4 Active fields — explicit allowlist drives both render + save. The
# field list is hand-curated so deprecated columns (entry_tick_buffer_bps,
# exit_tick_buffer_bps, max_hold_hours) are NEVER rendered. §13 + §16 L36.
PER_STRATEGY_FIELDS_PCT = ("entry_min_net_apy", "exit_min_net_apy")
PER_STRATEGY_FIELDS_RAW = (
    "exit_basis_buffer_multiple",
    "max_exit_basis_bps",
    "stop_loss_pct",
    "basis_dislocation_exit_bps",
    "min_position_pct",
    "max_position_pct",
    "sub_target_sizing_factor",
    "perp_leverage",
    "futures_buffer_pct",
    "depeg_guard_bps",
)
PER_STRATEGY_BOOLS = ("auto_transfer_enabled", "auto_quote_swap_enabled")

GLOBAL_FIELDS = (
    "max_open_positions",
    "max_trades_per_day",
    "loop_seconds",
    "cycle_error_rate_threshold",
)


def _per_strategy_dict(row: m.StrategyConfigPerStrategy) -> dict[str, object]:
    return {
        "entry_min_net_apy": row.entry_min_net_apy,
        "exit_min_net_apy": row.exit_min_net_apy,
        "exit_basis_buffer_multiple": row.exit_basis_buffer_multiple,
        "max_exit_basis_bps": row.max_exit_basis_bps,
        "stop_loss_pct": row.stop_loss_pct,
        "basis_dislocation_exit_bps": row.basis_dislocation_exit_bps,
        "min_position_pct": row.min_position_pct,
        "max_position_pct": row.max_position_pct,
        "sub_target_sizing_factor": row.sub_target_sizing_factor,
        "perp_leverage": row.perp_leverage,
        "futures_buffer_pct": row.futures_buffer_pct,
        "depeg_guard_bps": row.depeg_guard_bps,
        "auto_transfer_enabled": row.auto_transfer_enabled,
        "auto_quote_swap_enabled": row.auto_quote_swap_enabled,
    }


def _global_dict(row: m.StrategyConfig) -> dict[str, object]:
    return {
        "max_open_positions": row.max_open_positions,
        "max_trades_per_day": row.max_trades_per_day,
        "loop_seconds": row.loop_seconds,
        "cycle_error_rate_threshold": row.cycle_error_rate_threshold,
    }


@router.get("/config", response_class=HTMLResponse)
def config_get(
    request: Request,
    strategy: str = Query(default="binance_same_venue_funding_arb"),
    _user: str = Depends(require_basic_auth),
) -> HTMLResponse:
    with session_scope() as session:
        per_st_row = get_or_seed_per_strategy_config(session, strategy)
        glob_row = get_or_create_strategy_config(session)
        active_strategies = [
            r.trade_type for r in session.scalars(
                select(m.StrategyConfigPerStrategy).order_by(m.StrategyConfigPerStrategy.trade_type)
            ).all()
        ] or [strategy]
        per_strategy = _per_strategy_dict(per_st_row)
        global_cfg = _global_dict(glob_row)
    return templates.TemplateResponse(
        request,
        "config.html",
        {
            "request": request,
            "view_mode": get_view_mode(request),
            "saved": request.query_params.get("saved") == "1",
            "strategies": active_strategies,
            "active_strategy": strategy,
            "per_strategy": per_strategy,
            "global_cfg": global_cfg,
        },
    )


@router.post("/config")
async def config_post(request: Request, _user: str = Depends(require_basic_auth)) -> RedirectResponse:
    form = await request.form()
    strategy = str(form.get("strategy") or "binance_same_venue_funding_arb")
    with session_scope() as session:
        per_st = get_or_seed_per_strategy_config(session, strategy)
        glob = get_or_create_strategy_config(session)

        for fld in PER_STRATEGY_FIELDS_PCT:
            raw = form.get(f"{fld}_pct")
            if raw is None:
                continue
            try:
                setattr(per_st, fld, float(raw) / 100.0)
            except ValueError:
                pass
        for fld in PER_STRATEGY_FIELDS_RAW:
            raw = form.get(fld)
            if raw is None:
                continue
            try:
                cast = int if isinstance(getattr(per_st, fld), int) else float
                setattr(per_st, fld, cast(raw))
            except (ValueError, TypeError):
                pass
        for fld in PER_STRATEGY_BOOLS:
            setattr(per_st, fld, form.get(fld) == "1")

        for fld in GLOBAL_FIELDS:
            raw = form.get(fld)
            if raw is None:
                continue
            try:
                cast = int if isinstance(getattr(glob, fld), int) else float
                setattr(glob, fld, cast(raw))
            except (ValueError, TypeError):
                pass

    return RedirectResponse(url=f"/config?strategy={strategy}&saved=1", status_code=303)
