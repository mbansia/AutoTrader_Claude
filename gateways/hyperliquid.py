"""Hyperliquid gateway via ccxt. DEX wallet model per §6.4 + L11.

Per-venue specifics (§6.4 + §16 L11):
  - EVM auth: walletAddress + privateKey (no api key/secret). Sign-and-
    submit happens client-side via ccxt; the private key never leaves
    the process. Treat the env var as the most sensitive credential.
  - Single unified USDC collateral pool. consolidate_spot_wallets,
    transfer_spot_to_futures, transfer_futures_to_spot are all no-ops.
  - Funding settles HOURLY (interval_hours = 1). The bot's APY math
    auto-scales from the venue's reported interval; no code change needed,
    but operators must recalibrate `entry_min_net_apy` knowing that a
    hyperliquid candidate at the same per-window rate compounds to
    8× the APY of a Binance 8h candidate.
  - USDC-only quote: spot pairs are BASE/USDC, perps are BASE/USDC:USDC.
    Same-stable arbs only at this venue today.
  - No native dust-conversion. `convert_dust_to_native` is a no-op;
    sub-min naked legs sit as dust rows until value grows enough to trade
    out (or operator manually swaps).
  - Order placement: market+FOK is the primary path. If the venue's
    market order rejects the FOK TIF on a given symbol, the marketable-
    limit+FOK fallback (same pattern as Binance spot) kicks in via the
    shared exception path.

Not live-tested in this PR (§17 Stage 5). Wired per ccxt's hyperliquid
driver and the §6 spec; operator provisions creds at deploy.
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


class HyperliquidGateway:
    exchange_id: ExchangeId = "hyperliquid"

    def __init__(
        self,
        *,
        wallet_address: str,
        private_key: str,
        expected_account_id: str = "",
        marketable_limit_padding_bps: float = 100.0,
    ) -> None:
        self._client = ccxt.hyperliquid(
            {
                "walletAddress": wallet_address,
                "privateKey": private_key,
                "enableRateLimit": True,
            }
        )
        self._wallet_address = wallet_address
        self._expected_account_id = expected_account_id or wallet_address
        self._padding_bps = marketable_limit_padding_bps
        self._markets_loaded = False
        self._dedup = ErrorDedup()

    # ─── identity ──────────────────────────────────────────────────────
    def expected_account_id(self) -> str:
        return self._expected_account_id

    def actual_account_id(self) -> str:
        return self._wallet_address

    def probe_permissions(self) -> dict[str, bool]:
        """Hyperliquid is a single permission surface — the private key
        signs all order types. Probe by reading balance.
        """
        out = {"spot_read": False, "futures_read": False, "spot_trade": False, "futures_trade": False}
        try:
            self._client.fetch_balance()
            out["spot_read"] = True
            out["futures_read"] = True
            out["spot_trade"] = True
            out["futures_trade"] = True
        except Exception:  # noqa: BLE001
            pass
        return out

    def account_mode_probe(self) -> str:
        return "unified"

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
        return float(m.get("precision", {}).get("price", 0.0001))

    def lot_step(self, symbol: str) -> float:
        m = self._market(symbol)
        return float(m.get("precision", {}).get("amount", 0.0001))

    def min_notional(self, symbol: str) -> float:
        m = self._market(symbol)
        return float(m.get("limits", {}).get("cost", {}).get("min", 10.0) or 10.0)

    # ─── data ──────────────────────────────────────────────────────────
    def fetch_funding_rates(self) -> list[FundingInfo]:
        try:
            raw = self._client.fetch_funding_rates()
        except Exception as exc:  # noqa: BLE001
            if self._dedup.should_log(f"funding:{type(exc).__name__}"):
                log.warning("hyperliquid fetch_funding_rates failed: %r", exc)
            return []
        out: list[FundingInfo] = []
        for sym, info in raw.items():
            rate = info.get("fundingRate")
            if rate is None:
                continue
            # Hyperliquid pays funding HOURLY by venue policy. ccxt reports
            # `interval` as the string "1h" or as a 1-hour numeric where
            # the driver normalises it; trust the venue when present and
            # default to 1.0 hour to match HL's contract spec (§6.4 + L11).
            interval = info.get("interval", "1h")
            interval_h = 1.0
            if isinstance(interval, str) and interval.endswith("h"):
                try:
                    interval_h = float(interval[:-1])
                except ValueError:
                    interval_h = 1.0
            elif isinstance(interval, (int, float)):
                interval_h = float(interval)
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
        interval = info.get("interval", "1h")
        interval_h = 1.0
        if isinstance(interval, str) and interval.endswith("h"):
            try:
                interval_h = float(interval[:-1])
            except ValueError:
                interval_h = 1.0
        elif isinstance(interval, (int, float)):
            interval_h = float(interval)
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
        except Exception:  # noqa: BLE001
            # Hyperliquid taker tiers start ~3.5 bps; conservative default.
            return FeeInfo(spot_fee_bps=3.5, perp_fee_bps=3.5)
        spot_taker = float(fees.get(spot_symbol, {}).get("taker", 0.00035))
        perp_taker = float(fees.get(perp_symbol, {}).get("taker", 0.00035))
        return FeeInfo(spot_fee_bps=spot_taker * 10_000.0, perp_fee_bps=perp_taker * 10_000.0)

    def fetch_balance(self, asset: str) -> dict[VenueLeg, WalletBalance]:
        try:
            raw = self._client.fetch_balance()
        except Exception as exc:  # noqa: BLE001
            if self._dedup.should_log(f"balance:{type(exc).__name__}"):
                log.warning("hyperliquid fetch_balance failed: %r", exc)
            return {"spot": WalletBalance(0.0, 0.0), "futures": WalletBalance(0.0, 0.0)}
        a = raw.get(asset, {})
        free = float(a.get("free") or 0.0)
        total = float(a.get("total") or 0.0)
        # Unified pool: spot.free == futures.free (mirror), futures.total=0
        # so equity sums don't double-count. Same convention as Binance PM.
        return {
            "spot": WalletBalance(free, total),
            "futures": WalletBalance(free, 0.0),
        }

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
        """Marketable-limit+FOK on both legs (Hyperliquid's safest pattern;
        spot + perp both accept limit+FOK, the limit price is set 1% past
        top-of-book to guarantee crossing).
        """
        try:
            ob = self.snapshot_book(symbol, depth=5)
            if not ob.asks or not ob.bids:
                return FillResult(
                    symbol=symbol, venue_leg=venue_leg, side=side, qty=0.0,
                    avg_price=0.0, fee_quote=0.0, accepted=False,
                    error="empty_book", client_order_id=client_order_id,
                )
            pad = self._padding_bps / 10_000.0
            limit_price = ob.asks[0].price * (1.0 + pad) if side == "buy" else ob.bids[0].price * (1.0 - pad)
            order = self._client.create_order(
                symbol=symbol,
                type="limit",
                side=side,
                amount=qty,
                price=limit_price,
                params={"timeInForce": "FOK", "clientOrderId": client_order_id},
            )
            return order_to_fill_result(order, symbol=symbol, leg=venue_leg, side=side, coid=client_order_id)
        except (ccxt.InvalidOrder, ccxt.InsufficientFunds) as exc:
            return FillResult(
                symbol=symbol, venue_leg=venue_leg, side=side, qty=0.0,
                avg_price=0.0, fee_quote=0.0, accepted=False,
                error=f"{type(exc).__name__}:{exc}", client_order_id=client_order_id,
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
        # Unified pool — no buckets to consolidate.
        return {}

    def transfer_spot_to_futures(self, asset: str, amount: float) -> None:
        # Unified pool — no transfers needed between spot and perp.
        return None

    def transfer_futures_to_spot(self, asset: str, amount: float) -> None:
        return None

    def convert_dust_to_native(self, assets: list[str]) -> dict[str, float]:
        """Hyperliquid has no native dust-conversion endpoint. Sub-min
        positions persist until they can be normally traded out. §6.4 + L11.
        """
        return {a: 0.0 for a in assets}

    def list_capital_flow_records(self, lookback_days: int = 30) -> list[dict]:
        """§7.5 + §6.4: Hyperliquid deposits/withdrawals are L1 Arbitrum
        txs. ccxt's hyperliquid driver exposes them via `fetch_deposits` /
        `fetch_withdrawals` keyed on the wallet address.
        """
        from datetime import datetime, timedelta, timezone as _tz

        since_ms = int((datetime.now(_tz.utc) - timedelta(days=lookback_days)).timestamp() * 1000)
        rows: list[dict] = []
        try:
            deps = self._client.fetch_deposits("USDC", since=since_ms) or []
        except Exception:  # noqa: BLE001
            deps = []
        try:
            wdrs = self._client.fetch_withdrawals("USDC", since=since_ms) or []
        except Exception:  # noqa: BLE001
            wdrs = []
        for d in deps:
            ts = _ms_to_dt_hl(d.get("timestamp"))
            amt = float(d.get("amount") or 0.0)
            tx = str(d.get("txid") or d.get("id") or "")
            if not tx or amt <= 0.0:
                continue
            rows.append({
                "ts": ts, "amount_usdt": amt, "flow_type": "deposit",
                "external_id": f"hyperliquid:deposit:{tx}",
                "note": "Hyperliquid L1 deposit (Arbitrum)",
            })
        for w in wdrs:
            ts = _ms_to_dt_hl(w.get("timestamp"))
            amt = -abs(float(w.get("amount") or 0.0))
            tx = str(w.get("txid") or w.get("id") or "")
            if not tx or amt == 0.0:
                continue
            rows.append({
                "ts": ts, "amount_usdt": amt, "flow_type": "withdrawal",
                "external_id": f"hyperliquid:withdrawal:{tx}",
                "note": "Hyperliquid L1 withdrawal (Arbitrum)",
            })
        return rows


def _ms_to_dt_hl(ms):
    from datetime import datetime, timezone as _tz

    if ms is None:
        return datetime.now(_tz.utc)
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=_tz.utc)
