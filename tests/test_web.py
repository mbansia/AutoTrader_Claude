"""HTTP layer tests. /health (no auth), /api/diagnostics (token-gated)."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "secret123")
    import importlib
    import state.db as db_mod
    importlib.reload(db_mod)
    import diagnostics.endpoint as ep
    importlib.reload(ep)
    from web.app import create_app
    return TestClient(create_app())


def test_health_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_diagnostics_503_without_token_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'no_token.db'}")
    monkeypatch.delenv("DIAGNOSTICS_TOKEN", raising=False)
    import importlib
    import state.db as db_mod
    importlib.reload(db_mod)
    from web.app import create_app
    c = TestClient(create_app())
    r = c.get("/api/diagnostics?token=anything")
    assert r.status_code == 503


def test_diagnostics_401_on_bad_token(client):
    r = client.get("/api/diagnostics?token=wrong")
    assert r.status_code == 401


def test_diagnostics_200_with_correct_token(client):
    r = client.get("/api/diagnostics?token=secret123&hours=12")
    assert r.status_code == 200
    body = r.json()
    assert body["window_hours"] == 12
    assert body["generated_at_utc"].endswith("Z")
    assert "positions" in body
    assert "anomalies" in body
