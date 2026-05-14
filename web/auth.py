"""HTTP Basic auth dep, per §5. Single operator user, env-supplied creds."""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from core.config import load_env


_security = HTTPBasic(auto_error=False)


def require_basic_auth(
    creds: HTTPBasicCredentials | None = Depends(_security),
) -> str:
    """Returns the username on success; raises 401 otherwise. Constant-time
    compare on both fields to avoid leaking via timing.
    """
    env = load_env()
    if not env.dashboard_password:
        # Refuse to be silently public: same posture as /api/diagnostics.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DASHBOARD_PASSWORD not set; refusing to serve UI",
        )
    if creds is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = secrets.compare_digest(creds.username, env.dashboard_user)
    pass_ok = secrets.compare_digest(creds.password, env.dashboard_password)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username
