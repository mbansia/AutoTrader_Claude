"""End-to-end parity harness smoke. Both 'legacy' and 'new' are the same
v1.5 endpoint here — the harness should report parity OK, exercising the
fetch + normalize + diff path on real shapes.
"""

from __future__ import annotations

import importlib
import os

from fastapi.testclient import TestClient


def test_parity_self_compares_clean(tmp_path, monkeypatch):
    """v1.5 vs v1.5 → no drift (sanity check for the harness itself)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'paritest.db'}")
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "t")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "p")
    monkeypatch.setenv("BOT_WORKER_ENABLED", "0")
    import state.db as db_mod
    importlib.reload(db_mod)
    import diagnostics.endpoint as ep
    importlib.reload(ep)
    from web.app import create_app

    with TestClient(create_app()) as c:
        a = c.get("/api/diagnostics?token=t").json()
        b = c.get("/api/diagnostics?token=t").json()

    from scripts.diagnostics_parity import diff_keys, normalize

    deltas = diff_keys(normalize(a), normalize(b))
    assert deltas == [], f"unexpected diff: {deltas}"


def test_parity_detects_drift():
    """Inject a synthetic difference and verify the harness sees it."""
    from scripts.diagnostics_parity import diff_keys, normalize

    a = {
        "generated_at_utc": "2026-05-16T00:00:00Z",
        "cycle_health": {"error_count": 0},
        "positions": {"open": [{"symbol": "ABC", "quantity": 1.0}]},
    }
    b = {
        "generated_at_utc": "2026-05-16T01:00:00Z",  # ignored by normalize
        "cycle_health": {"error_count": 3},          # → drift
        "positions": {"open": [{"symbol": "ABC", "quantity": 1.0, "extra": "x"}]},
    }
    deltas = diff_keys(normalize(a), normalize(b))
    assert any("error_count" in d for d in deltas)
    assert any("extra" in d for d in deltas)
