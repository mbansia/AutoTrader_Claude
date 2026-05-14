"""Safety & Rules — read-only mirror of /config + mode-state toggles. §5.2."""

from __future__ import annotations

import socket

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from state import models as m
from state import session_scope
from state.repository import get_or_create_mode_state, get_or_create_strategy_config
from web.auth import require_basic_auth
from web.view_mode import get_view_mode


router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


def _outbound_ip() -> str:
    """Best-effort: get the local outbound IP (no DNS, no external call).
    Used for KuCoin API key IP whitelisting (§2.1)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "unknown"


@router.get("/safety", response_class=HTMLResponse)
def safety(request: Request, _user: str = Depends(require_basic_auth)) -> HTMLResponse:
    with session_scope() as session:
        modes = ("paper", "live")
        rows = [get_or_create_mode_state(session, mode) for mode in modes]
        ms_data = [
            {
                "mode": r.mode,
                "entry_enabled": r.entry_enabled,
                "exit_enabled": r.exit_enabled,
                "maintenance_mode": r.maintenance_mode,
            }
            for r in rows
        ]
        glob = get_or_create_strategy_config(session)
        per_st_rows = session.scalars(select(m.StrategyConfigPerStrategy)).all()
        guard: dict[str, str] = {
            "loop_seconds": str(glob.loop_seconds),
            "max_open_positions": str(glob.max_open_positions),
            "max_trades_per_day": str(glob.max_trades_per_day),
            "hedge_integrity_check": str(glob.hedge_integrity_check),
            "delisting_check": str(glob.delisting_check),
            "cycle_error_rate_threshold": str(glob.cycle_error_rate_threshold),
        }
        for r in per_st_rows:
            prefix = f"[{r.trade_type}]"
            guard[f"{prefix} entry_min_net_apy"] = f"{r.entry_min_net_apy:.2%}"
            guard[f"{prefix} exit_min_net_apy"] = f"{r.exit_min_net_apy:.2%}"
            guard[f"{prefix} basis_dislocation_exit_bps"] = str(r.basis_dislocation_exit_bps)
            guard[f"{prefix} sub_target_sizing_factor"] = str(r.sub_target_sizing_factor)
            guard[f"{prefix} stop_loss_pct"] = str(r.stop_loss_pct)
            guard[f"{prefix} perp_leverage"] = str(r.perp_leverage)
    return templates.TemplateResponse(
        request,
        "safety.html",
        {
            "request": request,
            "view_mode": get_view_mode(request),
            "saved": request.query_params.get("saved") == "1",
            "mode_states": ms_data,
            "guardrails": guard,
            "outbound_ip": _outbound_ip(),
        },
    )


@router.post("/safety/mode")
async def safety_mode_post(
    request: Request,
    mode: str = Form(...),
    _user: str = Depends(require_basic_auth),
) -> RedirectResponse:
    form = await request.form()
    with session_scope() as session:
        ms = get_or_create_mode_state(session, mode)
        ms.entry_enabled = form.get("entry_enabled") == "1"
        ms.exit_enabled = form.get("exit_enabled") == "1"
        ms.maintenance_mode = form.get("maintenance_mode") == "1"
    return RedirectResponse(url="/safety?saved=1", status_code=303)
