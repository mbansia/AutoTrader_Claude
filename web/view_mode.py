"""Session cookie tracking paper/live view toggle. §5.1."""

from __future__ import annotations

from typing import Literal

from fastapi import Request, Response

VIEW_COOKIE = "atc_view"


def get_view_mode(request: Request) -> Literal["paper", "live"]:
    v = request.cookies.get(VIEW_COOKIE, "paper")
    return "live" if v == "live" else "paper"


def set_view_mode(response: Response, mode: Literal["paper", "live"]) -> None:
    response.set_cookie(
        key=VIEW_COOKIE,
        value=mode,
        max_age=60 * 60 * 24 * 30,  # 30 days
        httponly=True,
        samesite="strict",
    )
