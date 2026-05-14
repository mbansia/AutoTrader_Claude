"""Binance gateway via ccxt. Portfolio Margin (PM) routing per §6.1.

Key per-venue quirks the spec calls out (§6.1 + §16 L11/L14/L15):
  - PM-only: balance reads via `/papi/v1/balance`; orders route through
    the unified PM endpoints. Spot wallet + USDM-futures + CM-futures all
    draw from one collateral pool.
  - Spot market orders DO NOT accept `timeInForce`. Use the "marketable-
    limit + FOK" fallback (§0.2 v1.4): submit a limit order with a price
    set far beyond top-of-book (default 1% past) with `timeInForce=FOK`.
    Effectively a market order with FOK semantics.
  - USDM-futures `market` orders DO accept `timeInForce=FOK` directly.
  - Filter pseudo-tokens (`LDUSDT`, `BFRBUSDT`) when reading spot balances
    (§16 L14).
  - Never trust string truthiness from balance responses (§16 L15) — parse
    to floats first.

This module is **not live-tested in this PR**; live cutover validation is
§17 Stage 5 work. The methods follow ccxt's documented surface and the
§6 spec exactly. Operators wire creds via BINANCE_API_KEY / _SECRET +
BINANCE_EXPECTED_ACCOUNT_ID.
"""

from __future__ import annotations

import logging
from typing import Any

import ccxt

from core.types import (
    BookSnapshot,
    ExchangeId,
    FeeInfo,
    FillResult,
    FundingInfo,
    Side,
    VenueLeg,
    WalletBalance,
    utcnow,
)

from ._ccxt_helpers import ErrorDedup, book_from_ccxt, order_to_fill_result


log = logging.getLogger(__name__)


_PSEUDO_TOKEN_PREFIXES = ("LD", "BFR")  # §16 L14


def _is_real_spot_asset(symbol: str) -> bool:
    """Filter Binance Earn/Lending pseudo-tokens from spot balance responses."""
    return not any(symbol.upper().startswith(p) and len(symbol) > len(p) for p in _PSEUDO_TOKEN_PREFIXES)


class BinanceGateway:
    """Conforms to gateways.base.Gateway via duck typing."""

    exchange_id: ExchangeId = "binance"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        expected_account_id: str = "",
        marketable_limit_padding_bps: float = 100.0,
    ) -> None:
        self._client = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future",
                    "portfolioMargin": True,
                },
            }
        )
        self._expected_account_id = expected_account_id
        self._padding_bps = marketable_limit_padding_bps
        self._markets_loaded = False
        self._error_dedup = ErrorDedup()

    # ─── identity ──────────────────────────────────────────────────────
    def expected_account_id(self) -> str:
        return self._expected_account_id

    def actual_account_id(self) -> str:
        try:
            info = self._client.fetch_account()
        except Exception as exc:  # noqa: BLE001
            if self._error_dedup.should_log(f"account:{type(exc).__name__}"):
                log.warning("binance fetch_account failed: %r", exc)
            return ""
        return str(info.get("info", {}).get("uid") or info.get("info", {}).get("accountAlias") or "")

    def probe_permissions(self) -> dict[str, bool]:
        out = {"spot_read": False, "futures_read": False, "spot_trade": False, "futures_trade": False}
        try:
            self._client.fetch_balance()
            out["spot_read"] = True
            out["futures_read"] = True
        except Exception:  # noqa: BLE001
            pass
        # Trade permission is inferred from balance permissions; a hard probe
        # would require placing + canceling a far-OTM limit, which has cost.
        out["spot_trade"] = out["spot_read"]
        out["futures_trade"] = out["futures_read"]
        return out

    def account_mode_probe(self) -> str:
        return "unified"  # PM is always unified

    # ─── markets ───────────────────────────────────────────────────────
    def load_markets(self, force: bool = False) -> None:
        if force or not self._markets_loaded:
            self._client.load_markets(reload=force)
            self._markets_loaded = True

    def _market(self, symbol: str) -> dict[str, Any]:
        self.load_markets()
        return self._client.markets[symbol]

    def tick_size(self, symbol: str) -> float:
        m = self._market(symbol)
        return float(m.get("precision", {}).get("price", 0.01))

    def lot_step(self, symbol: str) -> float:
        m = self._market(symbol)
        return float(m.get("precision", {}).get("amount", 0.001))

    def min_notional(self, symbol: str) -> float:
        m = self._market(symbol)
        return float(m.get("limits", {}).get("cost", {}).get("min", 5.0) or 5.0)

    # ─── data ──────────────────────────────────────────────────────────
    def fetch_funding_rates(self) -> list[FundingInfo]:
        try:
            raw = self._client.fetch_funding_rates()
        except Exception as exc:  # noqa: BLE001
            if self._error_dedup.should_log(f"funding:{type(exc).__name__}"):
                log.warning("binance fetch_funding_rates failed: %r", exc)
            return []
        out: list[FundingInfo] = []
        for sym, info in raw.items():
            rate = info.get("fundingRate")
            interval = info.get("interval", "8h")
            interval_h = 8.0
            if isinstance(interval, str) and interval.endswith("h"):
                try:
                    interval_h = float(interval[:-1])
                except ValueError:
                    pass
            if rate is None:
                continue
            out.append(
                FundingInfo(
                    symbol=sym,
                    predicted_rate=float(rate),
                    interval_hours=interval_h,
                    next_funding_ts=utcnow(),
                )
            )
        return out

    def fetch_predicted_funding(self, perp_symbol: str) -> FundingInfo:
        info = self._client.fetch_funding_rate(perp_symbol)
        rate = float(info.get("fundingRate") or 0.0)
        interval = info.get("interval", "8h")
        interval_h = 8.0
        if isinstance(interval, str) and interval.endswith("h"):
            try:
                interval_h = float(interval[:-1])
            except ValueError:
                pass
        return FundingInfo(
            symbol=perp_symbol,
            predicted_rate=rate,
            interval_hours=interval_h,
            next_funding_ts=utcnow(),
        )

    def snapshot_book(self, symbol: str, depth: int = 20) -> BookSnapshot:
        ob = self._client.fetch_order_book(symbol, limit=depth)
        return book_from_ccxt(ob, symbol)

    def fetch_fees(self, spot_symbol: str, perp_symbol: str) -> FeeInfo:
        try:
            fees = self._client.fetch_trading_fees()
        except Exception as exc:  # noqa: BLE001
            if self._error_dedup.should_log(f"fees:{type(exc).__name__}"):
                log.warning("binance fetch_trading_fees failed: %r", exc)
            return FeeInfo(spot_fee_bps=6.0, perp_fee_bps=6.0)
        spot_taker = float(fees.get(spot_symbol, {}).get("taker", 0.0006))
        perp_taker = float(fees.get(perp_symbol, {}).get("taker", 0.0006))
        return FeeInfo(spot_fee_bps=spot_taker * 10_000.0, perp_fee_bps=perp_taker * 10_000.0)

    def fetch_balance(self, asset: str) -> dict[VenueLeg, WalletBalance]:
        try:
            raw = self._client.fetch_balance(params={"type": "papi"})
        except Exception as exc:  # noqa: BLE001
            if self._error_dedup.should_log(f"balance:{type(exc).__name__}"):
                log.warning("binance fetch_balance failed: %r", exc)
            return {"spot": WalletBalance(0.0, 0.0), "futures": WalletBalance(0.0, 0.0)}

        # §16 L15: parse to float FIRST, never trust truthiness of strings.
        info = raw.get("info", {})
        cross_free = _parse_float(info.get("crossMarginFree"))
        um_wallet = _parse_float(info.get("umWalletBalance"))
        cm_wallet = _parse_float(info.get("cmWalletBalance"))
        # PM: every asset shares one collateral pool. Per §6.1 we synthesise
        # `futures.free == spot.free` and `futures.total == 0` so equity
        # sums don't double-count.
        spot_free = cross_free + um_wallet + cm_wallet
        if _is_real_spot_asset(asset):
            return {
                "spot": WalletBalance(spot_free, spot_free),
                "futures": WalletBalance(spot_free, 0.0),
            }
        return {"spot": WalletBalance(0.0, 0.0), "futures": WalletBalance(0.0, 0.0)}

    def list_open_perp_positions(self) -> list[tuple[str, float]]:
        try:
            positions = self._client.fetch_positions()
        except Exception:  # noqa: BLE001
            return []
        return [
            (p["symbol"], float(p.get("contracts") or 0.0) * (1 if p.get("side") == "long" else -1))
            for p in positions
            if float(p.get("contracts") or 0.0) != 0.0
        ]

    # ─── orders ────────────────────────────────────────────────────────
    def place_market_fok(
        self,
        symbol: str,
        venue_leg: VenueLeg,
        side: Side,
        qty: float,
        client_order_id: str,
    ) -> FillResult:
        """Market+FOK on futures; marketable-limit+FOK on spot.

        Binance spot doesn't accept timeInForce on market orders — the
        canonical arb-trader workaround is to submit a limit with a price
        set far beyond top-of-book (`marketable_limit_padding_bps`, default
        100 bps = 1%) with timeInForce=FOK. Behaves like market+FOK.
        """
        try:
            if venue_leg == "futures":
                order = self._client.create_order(
                    symbol=symbol,
                    type="market",
                    side=side,
                    amount=qty,
                    params={"timeInForce": "FOK", "newClientOrderId": client_order_id},
                )
            else:
                ob = self.snapshot_book(symbol, depth=5)
                if not ob.asks or not ob.bids:
                    return FillResult(
                        symbol=symbol, venue_leg=venue_leg, side=side, qty=0.0,
                        avg_price=0.0, fee_quote=0.0, accepted=False,
                        error="empty_book", client_order_id=client_order_id,
                    )
                # Marketable limit: price 1% past top-of-book on the adverse side.
                pad = self._padding_bps / 10_000.0
                if side == "buy":
                    limit_price = ob.asks[0].price * (1.0 + pad)
                else:
                    limit_price = ob.bids[0].price * (1.0 - pad)
                order = self._client.create_order(
                    symbol=symbol,
                    type="limit",
                    side=side,
                    amount=qty,
                    price=limit_price,
                    params={"timeInForce": "FOK", "newClientOrderId": client_order_id},
                )
            return order_to_fill_result(order, symbol=symbol, leg=venue_leg, side=side, coid=client_order_id)
        except ccxt.InvalidOrder as exc:
            return FillResult(
                symbol=symbol, venue_leg=venue_leg, side=side, qty=0.0,
                avg_price=0.0, fee_quote=0.0, accepted=False,
                error=f"invalid_order:{exc}", client_order_id=client_order_id,
            )
        except ccxt.InsufficientFunds as exc:
            return FillResult(
                symbol=symbol, venue_leg=venue_leg, side=side, qty=0.0,
                avg_price=0.0, fee_quote=0.0, accepted=False,
                error=f"insufficient_funds:{exc}", client_order_id=client_order_id,
            )
        except Exception as exc:  # noqa: BLE001
            return FillResult(
                symbol=symbol, venue_leg=venue_leg, side=side, qty=0.0,
                avg_price=0.0, fee_quote=0.0, accepted=False,
                error=f"exception:{type(exc).__name__}:{exc}",
                client_order_id=client_order_id,
            )

    # ─── wallets ───────────────────────────────────────────────────────
    def consolidate_spot_wallets(self, asset: str) -> dict[str, float]:
        # PM is already unified; no consolidation needed.
        return {}

    def transfer_spot_to_futures(self, asset: str, amount: float) -> None:
        # PM is no-op for transfers — single collateral pool.
        return None

    def transfer_futures_to_spot(self, asset: str, amount: float) -> None:
        return None

    def convert_dust_to_native(self, assets: list[str]) -> dict[str, float]:
        """Binance dust → BNB via `POST /sapi/v1/asset/dust`."""
        try:
            result = self._client.sapiPostAssetDust({"asset": assets})
            return {a: 0.0 for a in assets} | {"_result": result}  # type: ignore[dict-item]
        except Exception as exc:  # noqa: BLE001
            log.warning("binance dust conversion failed: %r", exc)
            return {a: 0.0 for a in assets}


def _parse_float(value: Any) -> float:
    """§16 L15: parse to float before any truthiness checks. The string
    `"0"` is truthy in Python; only a parsed 0.0 is honestly zero.
    """
    if value is None:
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0
