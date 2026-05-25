"""Capital-flow ingest: per-cycle pull from gateways into the CapitalFlow
table. Idempotent via `external_id` UNIQUE. Throttled to once per hour
per (mode, exchange). Dashboard net-injected-capital reads the table.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

import pytest


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cf.db'}")
    monkeypatch.setenv("BOT_WORKER_ENABLED", "0")
    import state.db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    # Reset throttle map between tests.
    import loop.runner as runner
    runner._last_capital_flow_ingest.clear()
    return db_mod


def test_ingest_writes_new_rows(fresh_db):
    from gateways.paper import InMemoryGateway
    from loop.runner import _ingest_capital_flows
    from state import models as m
    from state import session_scope

    gw = InMemoryGateway(exchange_id="binance")
    gw.capital_flow_records = [
        {
            "ts": datetime.now(timezone.utc),
            "amount_usdt": 500.0,
            "flow_type": "deposit",
            "external_id": "binance:deposit:dep-001",
            "note": "test deposit",
        },
        {
            "ts": datetime.now(timezone.utc),
            "amount_usdt": -100.0,
            "flow_type": "withdrawal",
            "external_id": "binance:withdrawal:wdr-001",
            "note": "test withdrawal",
        },
    ]
    with session_scope() as session:
        _ingest_capital_flows(session, gw, "live")
        rows = session.scalars(
            __import__("sqlalchemy").select(m.CapitalFlow)
        ).all()
        amounts = sorted(r.amount_usdt for r in rows)
        ext_ids = sorted(r.external_id for r in rows)
        kinds = sorted(r.kind for r in rows)
    assert amounts == [-100.0, 500.0]
    assert ext_ids == ["binance:deposit:dep-001", "binance:withdrawal:wdr-001"]
    # Legacy back-compat: `kind` column populated alongside `flow_type`.
    assert kinds == ["deposit", "withdrawal"]


def test_ingest_is_idempotent(fresh_db):
    """Calling ingest twice with the same records inserts only once."""
    from gateways.paper import InMemoryGateway
    from loop.runner import _ingest_capital_flows, _last_capital_flow_ingest
    from state import models as m
    from state import session_scope

    gw = InMemoryGateway(exchange_id="binance")
    gw.capital_flow_records = [
        {
            "ts": datetime.now(timezone.utc),
            "amount_usdt": 500.0,
            "flow_type": "deposit",
            "external_id": "binance:deposit:dup-test",
            "note": "first call",
        },
    ]
    with session_scope() as session:
        _ingest_capital_flows(session, gw, "live")
    # Defeat the per-(mode, exchange) throttle by clearing it.
    _last_capital_flow_ingest.clear()
    with session_scope() as session:
        _ingest_capital_flows(session, gw, "live")
        count = session.scalar(
            __import__("sqlalchemy").select(
                __import__("sqlalchemy").func.count(m.CapitalFlow.id)
            )
        )
    assert count == 1


def test_ingest_throttled_per_mode_exchange(fresh_db):
    """Two ingest calls within the throttle window: only the first runs."""
    from gateways.paper import InMemoryGateway
    from loop.runner import _ingest_capital_flows
    from state import models as m
    from state import session_scope

    gw = InMemoryGateway(exchange_id="binance")
    gw.capital_flow_records = [{
        "ts": datetime.now(timezone.utc),
        "amount_usdt": 100.0,
        "flow_type": "deposit",
        "external_id": "binance:deposit:throttle",
        "note": "throttle test",
    }]
    with session_scope() as session:
        _ingest_capital_flows(session, gw, "live")
    # Second call: throttle should bail before pulling records.
    gw.capital_flow_records = [{
        "ts": datetime.now(timezone.utc),
        "amount_usdt": 200.0,
        "flow_type": "deposit",
        "external_id": "binance:deposit:throttle-2",
        "note": "should NOT appear",
    }]
    with session_scope() as session:
        _ingest_capital_flows(session, gw, "live")
        count = session.scalar(
            __import__("sqlalchemy").select(
                __import__("sqlalchemy").func.count(m.CapitalFlow.id)
            )
        )
    assert count == 1


def test_ingest_swallows_gateway_failure(fresh_db, monkeypatch):
    """`list_capital_flow_records` raises → ingest logs + returns."""
    from gateways.paper import InMemoryGateway
    from loop.runner import _ingest_capital_flows
    from state import models as m
    from state import session_scope

    gw = InMemoryGateway(exchange_id="binance")

    def boom(*_args, **_kwargs):
        raise RuntimeError("venue offline")

    monkeypatch.setattr(gw, "list_capital_flow_records", boom)
    with session_scope() as session:
        _ingest_capital_flows(session, gw, "live")  # must not raise
        count = session.scalar(
            __import__("sqlalchemy").select(
                __import__("sqlalchemy").func.count(m.CapitalFlow.id)
            )
        )
    assert count == 0


def test_dashboard_net_injected_uses_ingested_flows(fresh_db, monkeypatch, tmp_path):
    """End-to-end: ingest writes flows, dashboard sums them as net-injected."""
    monkeypatch.setenv("DIAGNOSTICS_TOKEN", "t")
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    from gateways.paper import InMemoryGateway
    from loop.runner import _ingest_capital_flows
    from state import session_scope

    gw = InMemoryGateway(exchange_id="binance")
    gw.capital_flow_records = [
        {
            "ts": datetime.now(timezone.utc),
            "amount_usdt": 1000.0,
            "flow_type": "deposit",
            "external_id": "binance:deposit:e2e-1",
            "note": "e2e",
        },
        {
            "ts": datetime.now(timezone.utc),
            "amount_usdt": -250.0,
            "flow_type": "withdrawal",
            "external_id": "binance:withdrawal:e2e-1",
            "note": "e2e",
        },
    ]
    with session_scope() as session:
        _ingest_capital_flows(session, gw, "live")

    from fastapi.testclient import TestClient
    from web.app import create_app
    c = TestClient(create_app())
    c.post("/view/live", auth=("admin", "pw"), follow_redirects=False)
    r = c.get("/dashboard", auth=("admin", "pw"))
    assert r.status_code == 200
    # Net injected capital: 1000 + (-250) = $750.00
    assert "$750.00" in r.text


def test_paper_gateway_returns_empty_records_by_default():
    """Paper gateway with no fixture data returns empty list."""
    from gateways.paper import InMemoryGateway
    gw = InMemoryGateway(exchange_id="binance")
    assert gw.list_capital_flow_records() == []
