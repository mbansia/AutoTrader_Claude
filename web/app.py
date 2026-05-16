"""FastAPI app — v1.4 entrypoint. Mounts every UI page per §5 + the two
contractual endpoints (§5.6 /health, §8.1 /api/diagnostics).
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from diagnostics import build_snapshot
from state import init_db, session_scope


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Startup: kick the bot loop unless the operator opts out (API-only
    # replicas set BOT_WORKER_ENABLED=0).
    from loop import start_runner, stop_runner

    start_runner()
    try:
        yield
    finally:
        # Graceful SIGTERM — finish in-flight cycles, then exit.
        stop_runner(timeout_s=10.0)


def create_app() -> FastAPI:
    app = FastAPI(
        title="AutoTrader_Codex (v1.5)",
        version="1.5.0",
        lifespan=_lifespan,
    )
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

    # Static + UI routes.
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    from web.routes.dashboard import router as dashboard_router
    from web.routes.monitoring import router as monitoring_router
    from web.routes.config_routes import router as config_router
    from web.routes.safety import router as safety_router
    from web.routes.view import router as view_router

    app.include_router(dashboard_router)
    app.include_router(monitoring_router)
    app.include_router(config_router)
    app.include_router(safety_router)
    app.include_router(view_router)

    return app


app = create_app()
