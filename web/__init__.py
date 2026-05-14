"""FastAPI surface (v1.4 rewrite). `/health` + `/api/diagnostics` ship here;
HTML pages stay in the legacy `app/` package until cutover (§17 Stage 6).
"""

from .app import app, create_app
