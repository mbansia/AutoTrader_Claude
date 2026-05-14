"""View-mode toggle. POST /view/{mode} flips the session cookie. §5.1."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from web.auth import require_basic_auth
from web.view_mode import set_view_mode


router = APIRouter()


@router.post("/view/{mode}")
def view_toggle(request: Request, mode: str, _user: str = Depends(require_basic_auth)) -> RedirectResponse:
    target = "live" if mode == "live" else "paper"
    referer = request.headers.get("referer", "/dashboard")
    response = RedirectResponse(url=referer, status_code=303)
    set_view_mode(response, target)
    return response
