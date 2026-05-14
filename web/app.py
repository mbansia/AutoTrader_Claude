"""FastAPI app. Frozen surface: `/health` (no auth) + `/api/diagnostics`
(?token=). The UI HTML routes (dashboard, transactions, etc.) are kept in
the legacy `app/` package until the cutover; this module ships the two
contractual endpoints from §5.6 + §8.1.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from diagnostics import build_snapshot
from state import init_db, session_scope


def create_app() -> FastAPI:
    app = FastAPI(title="AutoTrader_Codex (v1.4 rewrite)", version="1.4.0")
    init_db()

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/api/diagnostics")
    def diagnostics(
        token: str | None = Query(default=None),
        hours: int = Query(default=24),
    ) -> Any:
        expected = os.environ.get("DIAGNOSTICS_TOKEN")
        if not expected:
            return JSONResponse(
                status_code=503,
                content={"error": "DIAGNOSTICS_TOKEN not set; refusing to be silently public"},
            )
        if token != expected:
            raise HTTPException(status_code=401, detail="invalid_token")
        with session_scope() as s:
            return build_snapshot(s, hours_raw=hours)

    return app


app = create_app()
