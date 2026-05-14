"""Test fixtures: in-memory SQLite + InMemoryGateway, both reset per-test."""

from __future__ import annotations

import os
import sys
import pytest

# Make sibling packages importable when pytest runs from repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy.orm import Session, sessionmaker

from state.db import build_engine, run_migrations
from state.models import Base

from gateways.paper import InMemoryGateway


@pytest.fixture()
def engine():
    eng = build_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    run_migrations(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine) -> Session:
    Factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    s = Factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


@pytest.fixture()
def gateway() -> InMemoryGateway:
    return InMemoryGateway(exchange_id="binance", slippage_bps=0.0, fee_bps=0.0)
