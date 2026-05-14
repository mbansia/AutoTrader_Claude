"""Diagnostics endpoint shape + anomaly rules. §8.1 frozen contract."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from diagnostics import build_snapshot, detect_anomalies
from state import models as m
from state.repository import (
    get_or_create_mode_state,
    get_or_seed_per_strategy_config,
    insert_rejection,
    log_event,
)


def _seed_mode(session):
    get_or_create_mode_state(session, "paper")
    get_or_seed_per_strategy_config(session, "binance_same_venue_funding_arb")


def test_snapshot_shape_complete(session):
    _seed_mode(session)
    log_event(session, mode="paper", exchange="binance", level="INFO", message="boot")
    snap = build_snapshot(session, hours_raw=24)

    # Top-level required keys
    for key in [
        "generated_at_utc",
        "window_hours",
        "cycle_health",
        "positions",
        "wallets",
        "rejections_grouped",
        "rejections_total",
        "recent_events",
        "recent_trades",
        "recent_trades_count",
        "anomalies",
        "anomalies_count",
    ]:
        assert key in snap, f"missing key: {key}"

    # positions has the required sub-keys
    assert "by_status" in snap["positions"]
    assert "open" in snap["positions"]
    assert "naked" in snap["positions"]


def test_iso8601_z_suffix(session):
    _seed_mode(session)
    log_event(session, mode="paper", exchange="binance", level="INFO", message="t")
    snap = build_snapshot(session, hours_raw=24)
    assert snap["generated_at_utc"].endswith("Z")
    if snap["cycle_health"]["last_event_ts"] is not None:
        assert snap["cycle_health"]["last_event_ts"].endswith("Z")


def test_hours_clamped(session):
    _seed_mode(session)
    snap_low = build_snapshot(session, hours_raw=-5)
    assert snap_low["window_hours"] == 1
    snap_high = build_snapshot(session, hours_raw=10_000)
    assert snap_high["window_hours"] == 168


def test_no_recent_events_anomaly(session):
    """No BotEvent rows → critical no_recent_events."""
    _seed_mode(session)
    snap = build_snapshot(session, hours_raw=24)
    rules = [a["rule"] for a in snap["anomalies"]]
    assert "no_recent_events" in rules


def test_stale_naked_spot_and_perp_anomalies(session):
    _seed_mode(session)
    long_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    p1 = m.Position(
        mode="paper",
        exchange="binance",
        trade_type="binance_same_venue_funding_arb",
        status="naked_spot",
        symbol="ABC",
        spot_symbol="ABC/USDT",
        perp_symbol="ABC/USDT:USDT",
        quote_currency="USDT",
        spot_quote_currency="USDT",
        quantity=1.0,
        opened_at=long_ago,
        spot_entry_price=100.0,
        perp_entry_price=0.0,
    )
    p2 = m.Position(
        mode="paper",
        exchange="binance",
        trade_type="binance_same_venue_funding_arb",
        status="naked_perp",
        symbol="XYZ",
        spot_symbol="XYZ/USDT",
        perp_symbol="XYZ/USDT:USDT",
        quote_currency="USDT",
        spot_quote_currency="USDT",
        quantity=2.0,
        opened_at=long_ago,
        spot_entry_price=0.0,
        perp_entry_price=50.0,
    )
    session.add_all([p1, p2])
    log_event(session, mode="paper", exchange="binance", level="INFO", message="recent")
    session.flush()

    snap = build_snapshot(session, hours_raw=24)
    rules = [a["rule"] for a in snap["anomalies"]]
    assert "stale_naked_spot" in rules
    assert "stale_naked_perp" in rules


def test_naked_position_payload_carries_status_field(session):
    """v1.1+: NakedPosition JSON has `status` so consumer disambiguates."""
    _seed_mode(session)
    p1 = m.Position(
        mode="paper",
        exchange="binance",
        trade_type="binance_same_venue_funding_arb",
        status="naked_perp",
        symbol="XYZ",
        spot_symbol="XYZ/USDT",
        perp_symbol="XYZ/USDT:USDT",
        quote_currency="USDT",
        spot_quote_currency="USDT",
        quantity=2.0,
        opened_at=datetime.now(timezone.utc),
        spot_entry_price=0.0,
        perp_entry_price=50.0,
    )
    session.add(p1)
    log_event(session, mode="paper", exchange="binance", level="INFO", message="x")
    session.flush()
    snap = build_snapshot(session, hours_raw=24)
    assert len(snap["positions"]["naked"]) == 1
    assert snap["positions"]["naked"][0]["status"] == "naked_perp"
    assert snap["positions"]["naked"][0]["leg_entry_price"] == 50.0


def test_no_trades_despite_scans_anomaly(session):
    _seed_mode(session)
    log_event(session, mode="paper", exchange="binance", level="INFO", message="active")
    for i in range(25):
        insert_rejection(
            session,
            mode="paper",
            exchange="binance",
            symbol=f"X{i}",
            reason="insufficient_annualized_profit",
        )
    session.flush()
    snap = build_snapshot(session, hours_raw=24)
    rules = [a["rule"] for a in snap["anomalies"]]
    assert "no_trades_despite_scans" in rules


def test_cycle_error_rate_anomaly():
    """v1.4: error/cycle > 0.10 → critical."""
    snap = {
        "cycle_health": {"seconds_since_last_event": 30},
        "positions": {"naked": [], "open": []},
        "recent_trades_count": 0,
        "rejections_total": 0,
    }
    out = detect_anomalies(
        snapshot=snap,
        error_count=20,
        cycle_count_estimate=100,
        cycle_error_rate_threshold=0.10,
    )
    assert any(r["rule"] == "cycle_error_rate_high" for r in out)


def test_snapshot_empty_db_no_crash(session):
    """Pre-bootstrap edge case: no positions, no events, no trades. The
    snapshot must still produce a valid shape (no_recent_events anomaly fires).
    """
    snap = build_snapshot(session, hours_raw=24)
    assert snap["positions"]["by_status"]["open"] == 0
    assert snap["positions"]["open"] == []
    assert snap["positions"]["naked"] == []
    assert snap["recent_events"] == []
    assert snap["recent_trades_count"] == 0
    rules = [a["rule"] for a in snap["anomalies"]]
    assert "no_recent_events" in rules


def test_cycle_error_rate_below_threshold_no_anomaly():
    snap = {
        "cycle_health": {"seconds_since_last_event": 30},
        "positions": {"naked": [], "open": []},
        "recent_trades_count": 0,
        "rejections_total": 0,
    }
    out = detect_anomalies(
        snapshot=snap,
        error_count=5,
        cycle_count_estimate=100,
        cycle_error_rate_threshold=0.10,
    )
    assert all(r["rule"] != "cycle_error_rate_high" for r in out)
