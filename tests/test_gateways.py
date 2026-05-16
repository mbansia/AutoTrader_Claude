"""Gateway protocol conformance + ccxt-helper unit tests.

Live `BinanceGateway` + `KuCoinGateway` can't be tested without venue
credentials. We verify:
  - the protocol surface (every required method exists)
  - the ccxt response → FillResult mapping
  - the book-from-ccxt converter
  - the ErrorDedup throttle
  - the binance pseudo-token filter
"""

from __future__ import annotations

import time

import pytest

from gateways import BinanceGateway, HyperliquidGateway, InMemoryGateway, KuCoinGateway
from gateways._ccxt_helpers import ErrorDedup, book_from_ccxt, order_to_fill_result
from gateways.base import Gateway
from gateways.binance import _is_real_spot_asset, _parse_float


PROTOCOL_METHODS = (
    "expected_account_id",
    "actual_account_id",
    "probe_permissions",
    "account_mode_probe",
    "load_markets",
    "tick_size",
    "lot_step",
    "min_notional",
    "fetch_funding_rates",
    "fetch_predicted_funding",
    "snapshot_book",
    "fetch_fees",
    "fetch_balance",
    "list_open_perp_positions",
    "place_market_fok",
    "consolidate_spot_wallets",
    "transfer_spot_to_futures",
    "transfer_futures_to_spot",
    "convert_dust_to_native",
)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: InMemoryGateway(),
        lambda: InMemoryGateway(exchange_id="hyperliquid"),
        # The live gateways construct ccxt clients in __init__ — that's safe
        # (no network) but probes that need creds will fail. We just check
        # the method surface.
        lambda: BinanceGateway(api_key="x", api_secret="y"),
        lambda: KuCoinGateway(api_key="x", api_secret="y", passphrase="z"),
        # Hyperliquid wants a privateKey shaped like a 32-byte hex string
        # for ccxt's wallet init; the dummy below satisfies the constructor
        # without making any network call.
        lambda: HyperliquidGateway(
            wallet_address="0x0000000000000000000000000000000000000001",
            private_key="0x" + "11" * 32,
        ),
    ],
)
def test_protocol_surface(factory):
    gw = factory()
    for method in PROTOCOL_METHODS:
        assert hasattr(gw, method), f"missing {method}"
    # exchange_id should be a recognised value
    assert gw.exchange_id in {"binance", "kucoin", "hyperliquid"}


def test_hyperliquid_no_native_transfers():
    """§6.4: unified pool — transfers + consolidate are explicit no-ops."""
    gw = HyperliquidGateway(
        wallet_address="0x" + "00" * 20,
        private_key="0x" + "11" * 32,
    )
    assert gw.consolidate_spot_wallets("USDC") == {}
    assert gw.transfer_spot_to_futures("USDC", 100.0) is None
    assert gw.transfer_futures_to_spot("USDC", 100.0) is None


def test_hyperliquid_no_native_dust_conversion():
    """§6.4 + L11: HL has no dust endpoint; the call is a no-op."""
    gw = HyperliquidGateway(
        wallet_address="0x" + "00" * 20,
        private_key="0x" + "11" * 32,
    )
    out = gw.convert_dust_to_native(["DOGE", "SHIB"])
    assert out == {"DOGE": 0.0, "SHIB": 0.0}


def test_hyperliquid_account_id_defaults_to_wallet_address():
    """Operator can omit HYPERLIQUID_EXPECTED_ACCOUNT_ID; the wallet
    address itself is the expected id (the address IS the account)."""
    gw = HyperliquidGateway(
        wallet_address="0xabc",
        private_key="0x" + "11" * 32,
        expected_account_id="",
    )
    assert gw.expected_account_id() == "0xabc"
    assert gw.actual_account_id() == "0xabc"
    assert gw.account_mode_probe() == "unified"


def test_book_from_ccxt_converts_levels():
    ob = {
        "asks": [[100.0, 1.0], [101.0, 2.0]],
        "bids": [[99.5, 5.0], [99.0, 3.0]],
        "timestamp": 1715000000_000,
    }
    snap = book_from_ccxt(ob, "BTC/USDT")
    assert snap.symbol == "BTC/USDT"
    assert len(snap.asks) == 2 and snap.asks[0].price == 100.0
    assert len(snap.bids) == 2 and snap.bids[0].price == 99.5
    assert snap.mid_price == (100.0 + 99.5) / 2
    assert snap.taken_at.tzinfo is not None


def test_order_to_fill_result_success():
    order = {"status": "closed", "filled": 0.5, "average": 100.0, "fee": {"cost": 0.05}}
    r = order_to_fill_result(order, symbol="X/USDT", leg="spot", side="buy", coid="c1")
    assert r.accepted
    assert r.qty == 0.5
    assert r.avg_price == 100.0
    assert r.fee_quote == 0.05


def test_order_to_fill_result_rejected():
    order = {"status": "canceled", "filled": 0.0}
    r = order_to_fill_result(order, symbol="X/USDT", leg="spot", side="buy", coid="c1")
    assert not r.accepted
    assert "fok_rejected" in (r.error or "")


def test_order_to_fill_result_empty():
    r = order_to_fill_result(None, symbol="X", leg="spot", side="buy", coid="c1")
    assert not r.accepted
    assert r.error == "empty_response"


def test_error_dedup_throttles():
    d = ErrorDedup(window_seconds=60)
    assert d.should_log("k1")
    assert not d.should_log("k1")
    assert d.should_log("k2")


def test_binance_pseudo_token_filter():
    """§16 L14: LDUSDT, BFRBUSDT are Earn/Lending pseudo-tokens, not tradable."""
    assert _is_real_spot_asset("USDT")
    assert _is_real_spot_asset("BTC")
    assert not _is_real_spot_asset("LDUSDT")
    assert not _is_real_spot_asset("BFRBUSDT")


def test_binance_parse_float_l15():
    """§16 L15: parse-then-truthy, never trust string truthiness."""
    assert _parse_float("0") == 0.0
    assert _parse_float("0.5") == 0.5
    assert _parse_float(None) == 0.0
    assert _parse_float("") == 0.0
    assert _parse_float("garbage") == 0.0
