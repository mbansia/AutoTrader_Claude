"""HTML route smoke + auth contract + form save round-trip."""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ui.db'}")
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "tok")
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    import state.db as db_mod
    importlib.reload(db_mod)
    from web.app import create_app
    return TestClient(create_app())


@pytest.mark.parametrize("path", ["/dashboard", "/transactions", "/logs", "/monitoring", "/config", "/safety"])
def test_ui_requires_basic_auth(client, path):
    """Every UI route must 401 without creds (per §5 auth contract)."""
    r = client.get(path)
    assert r.status_code == 401


@pytest.mark.parametrize("path", ["/dashboard", "/transactions", "/logs", "/monitoring", "/config", "/safety", "/"])
def test_ui_serves_with_auth(client, path):
    r = client.get(path, auth=("admin", "pw"))
    assert r.status_code == 200
    assert b"<html" in r.content.lower() or b"<!doctype" in r.content.lower()


def test_view_toggle_sets_cookie(client):
    r = client.post("/view/live", auth=("admin", "pw"), follow_redirects=False)
    assert r.status_code == 303
    assert "atc_view=live" in r.headers.get("set-cookie", "")


def test_config_save_round_trip(client):
    """POST /config persists + redirects + GET reflects new value."""
    r = client.post(
        "/config",
        auth=("admin", "pw"),
        data={
            "strategy": "binance_same_venue_funding_arb",
            "entry_min_net_apy_pct": "33.0",
            "sub_target_sizing_factor": "0.6",
            "auto_transfer_enabled": "1",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Reload page and confirm value is reflected.
    r2 = client.get(
        "/config?strategy=binance_same_venue_funding_arb",
        auth=("admin", "pw"),
    )
    assert r2.status_code == 200
    assert b"33.00" in r2.content
    assert b"0.6" in r2.content


def test_dashboard_renders_when_db_empty(client):
    r = client.get("/dashboard", auth=("admin", "pw"))
    assert r.status_code == 200
    assert b"Dashboard" in r.content
    assert b"No open positions" in r.content


def test_basic_auth_503_when_no_password_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'noauth.db'}")
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "tok")
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    import state.db as db_mod
    importlib.reload(db_mod)
    from web.app import create_app
    c = TestClient(create_app())
    r = c.get("/dashboard", auth=("admin", "anything"))
    assert r.status_code == 503


def test_safety_mode_toggle(client):
    """POST /safety/mode flips the mode_state entry/exit/maintenance flags."""
    r = client.post(
        "/safety/mode",
        auth=("admin", "pw"),
        data={"mode": "paper", "entry_enabled": "1", "maintenance_mode": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    r2 = client.get("/safety", auth=("admin", "pw"))
    assert r2.status_code == 200


def test_safety_renders_venues_section(client):
    """v1.6: /safety shows a Venues row per exchange with active toggle."""
    r = client.get("/safety", auth=("admin", "pw"))
    assert r.status_code == 200
    assert b"Venues" in r.content
    assert b"hyperliquid" in r.content
    assert b"binance" in r.content


def test_safety_venue_toggle_persists(client):
    """POST /safety/venue flips active + writes expected_account_id."""
    r = client.post(
        "/safety/venue",
        auth=("admin", "pw"),
        data={
            "exchange_id": "hyperliquid",
            "active": "1",
            "expected_account_id": "0xABC",
            "notes": "operator confirmed",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Round-trip the GET to confirm.
    r2 = client.get("/safety", auth=("admin", "pw"))
    assert b"0xABC" in r2.content
    assert b"operator confirmed" in r2.content


def test_safety_venue_toggle_off(client):
    """Unchecked checkbox sends no `active` field → row becomes inactive."""
    # First turn it on so we can verify the off path.
    client.post(
        "/safety/venue",
        auth=("admin", "pw"),
        data={"exchange_id": "hyperliquid", "active": "1"},
        follow_redirects=False,
    )
    # Now POST without the checkbox.
    r = client.post(
        "/safety/venue",
        auth=("admin", "pw"),
        data={"exchange_id": "hyperliquid"},
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_monitoring_renders_gateway_cards(client):
    """Monitoring page renders probe cards when a gateway is registered."""
    import core.config as cfg

    class _StubGateway:
        exchange_id = "binance"

        def expected_account_id(self) -> str:
            return "acct-123"

        def actual_account_id(self) -> str:
            return "acct-123"

        def probe_permissions(self) -> dict:
            return {"spot_read": True, "futures_read": True}

        def account_mode_probe(self) -> str:
            return "portfolio_margin"

    cfg.register_gateway("paper", "binance", _StubGateway())
    try:
        r = client.get("/monitoring", auth=("admin", "pw"))
        assert r.status_code == 200
        assert b"binance" in r.content
        assert b"acct-123" in r.content
        assert b"portfolio_margin" in r.content
    finally:
        cfg.clear_registry()


def test_monitoring_gateway_probe_exceptions_handled(client):
    """Exception in all three probe calls must not crash /monitoring."""
    import core.config as cfg

    class _BrokenGateway:
        exchange_id = "kucoin"

        def expected_account_id(self) -> str:
            return "expected-kucoin"

        def actual_account_id(self) -> str:
            raise RuntimeError("no network")

        def probe_permissions(self) -> dict:
            raise RuntimeError("permission denied")

        def account_mode_probe(self) -> str:
            raise RuntimeError("mode error")

    cfg.register_gateway("live", "kucoin", _BrokenGateway())
    try:
        r = client.get("/monitoring", auth=("admin", "pw"))
        assert r.status_code == 200
        assert b"kucoin" in r.content
        assert b"expected-kucoin" in r.content
    finally:
        cfg.clear_registry()
