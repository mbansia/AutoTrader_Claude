"""Monitoring page — per-gateway probe cards. §5.2."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core.config import list_gateways
from web.auth import require_basic_auth
from web.view_mode import get_view_mode


router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


@router.get("/monitoring", response_class=HTMLResponse)
def monitoring(request: Request, _user: str = Depends(require_basic_auth)) -> HTMLResponse:
    cards = []
    for mode, exchange_id, gw in list_gateways():
        try:
            probes = gw.probe_permissions()
        except Exception as exc:  # noqa: BLE001
            probes = {"error": False, "detail": str(exc)[:80]}
        try:
            actual = gw.actual_account_id()
        except Exception:  # noqa: BLE001
            actual = ""
        try:
            am = gw.account_mode_probe()
        except Exception:  # noqa: BLE001
            am = ""
        cards.append(
            {
                "mode": mode,
                "exchange_id": exchange_id,
                "probes": probes,
                "expected_account_id": gw.expected_account_id(),
                "actual_account_id": actual,
                "account_mode": am,
            }
        )
    return templates.TemplateResponse(
        request,
        "monitoring.html",
        {
            "request": request,
            "view_mode": get_view_mode(request),
            "saved": False,
            "gateways": cards,
        },
    )
