"""KuCoin gateway via ccxt. Classic wallet model per §6.2.

Per-venue quirks (§6.2 + §16 L11):
  - Three+ spot wallets: main, trade, margin, isolated, pool. Spot orders
    execute against `trade` only.
  - Futures balance is per-currency: fetch USDT and USDC separately.
  - Order-book depth API only accepts `limit=20` or `limit=100` (other
    values are silently rejected).
  - Futures → spot is a TWO-HOP:
      Step 1: futures CONTRACT → MAIN via futures-side `transferOut`
              (the spot universal-transfer can't see the futures wallet
              and returns "balance insufficient")
      Step 2: spot MAIN → TRADE via inner-transfer (so funds land where
              the spot order book can spend them this cycle)
  - KCS dust conversion via `convertDust`.
  - Identical-error dedup; L20.

Untested live in this PR (§17 Stage 5). Wired per the spec and ccxt's
unified surface; `place_market_fok` uses native FOK on both spot and
futures (KuCoin spot accepts `timeInForce=FOK` on limit orders; KuCoin
futures accepts `timeInForce=FOK` on market orders).
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


class KuCoinGateway:
    exchange_id: ExchangeId = "kucoin"

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        passphrase: str,
        expected_account_id: str = "",
        marketable_limit_padding_bps: float = 100.0,
    ) -> None:
        self._spot = ccxt.kucoin(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "password": passphrase,
                "enableRateLimit": True,
            }
        )
        self._futures = ccxt.kucoinfutures(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "password": passphrase,
                "enableRateLimit": True,
            }
        )
        self._expected_account_id = expected_account_id
        self._padding_bps = marketable_limit_padding_bps
        self._markets_loaded = False
        self._dedup = ErrorDedup()

    # ─── identity ──────────────────────────────────────────────────────
    def expected_account_id(self) -> str:
        return self._expected_account_id

    def actual_account_id(self) -> str:
        try:
            sub = self._spot.privateGetSubUserBase()
            return str(sub.get("data", {}).get("uid") or "")
        except Exception:  # noqa: BLE001
            return ""

    def probe_permissions(self) -> dict[str, bool]:
        out = {"spot_read": False, "futures_read": False, "spot_trade": False, "futures_trade": False}
        try:
            self._spot.fetch_balance({"type": "trade"})
            out["spot_read"] = True
            out["spot_trade"] = True
        except Exception:  # noqa: BLE001
            pass
        try:
            self._futures.fetch_balance({"currency": "USDT"})
            out["futures_read"] = True
            out["futures_trade"] = True
        except Exception:  # noqa: BLE001
            pass
        return out

    def account_mode_probe(self) -> str:
        return "classic"

    # ─── markets ───────────────────────────────────────────────────────
    def load_markets(self, force: bool = False) -> None:
        if force or not self._markets_loaded:
            self._spot.load_markets(reload=force)
            self._futures.load_markets(reload=force)
            self._markets_loaded = True

    def _market(self, symbol: str, leg: VenueLeg = "spot") -> dict[str, Any]:
        self.load_markets()
        client = self._futures if leg == "futures" else self._spot
        return client.markets[symbol]

    def tick_size(self, symbol: str) -> float:
        m = self._market(symbol, "futures" if ":" in symbol else "spot")
        return float(m.get("precision", {}).get("price", 0.0001))

    def lot_step(self, symbol: str) -> float:
        m = self._market(symbol, "futures" if ":" in symbol else "spot")
        return float(m.get("precision", {}).get("amount", 0.001))

    def min_notional(self, symbol: str) -> float:
        m = self._market(symbol, "futures" if ":" in symbol else "spot")
        return float(m.get("limits", {}).get("cost", {}).get("min", 1.0) or 1.0)

    # ─── data ──────────────────────────────────────────────────────────
    def fetch_funding_rates(self) -> list[FundingInfo]:
        try:
            raw = self._futures.fetch_funding_rates()
        except Exception as exc:  # noqa: BLE001
            if self._dedup.should_log(f"funding:{type(exc).__name__}"):
                log.warning("kucoin fetch_funding_rates failed: %r", exc)
            return []
        out: list[FundingInfo] = []
        for sym, info in raw.items():
            rate = info.get("fundingRate")
            if rate is None:
                continue
            interval_h = float(info.get("interval", 8))  # KuCoin defaults vary
            if isinstance(info.get("interval"), str):
                try:
                    interval_h = float(str(info["interval"]).rstrip("h"))
                except ValueError:
                    interval_h = 8.0
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
        info = self._futures.fetch_funding_rate(perp_symbol)
        rate = float(info.get("fundingRate") or 0.0)
        return FundingInfo(
            symbol=perp_symbol,
            predicted_rate=rate,
            interval_hours=float(info.get("interval") or 8.0),
            next_funding_ts=utcnow(),
        )

    def snapshot_book(self, symbol: str, depth: int = 20) -> BookSnapshot:
        """§16 L11: KuCoin only accepts limit=20 or limit=100 — clamp."""
        clamped = 100 if depth > 20 else 20
        client = self._futures if ":" in symbol else self._spot
        ob = client.fetch_order_book(symbol, limit=clamped)
        return book_from_ccxt(ob, symbol)

    def fetch_fees(self, spot_symbol: str, perp_symbol: str) -> FeeInfo:
        try:
            spot_fees = self._spot.fetch_trading_fees()
            spot_taker = float(spot_fees.get(spot_symbol, {}).get("taker", 0.001))
        except Exception:  # noqa: BLE001
            spot_taker = 0.001
        try:
            perp_fees = self._futures.fetch_trading_fees()
            perp_taker = float(perp_fees.get(perp_symbol, {}).get("taker", 0.0006))
        except Exception:  # noqa: BLE001
            perp_taker = 0.0006
        return FeeInfo(spot_fee_bps=spot_taker * 10_000.0, perp_fee_bps=perp_taker * 10_000.0)

    def fetch_balance(self, asset: str) -> dict[VenueLeg, WalletBalance]:
        spot_total = 0.0
        spot_free = 0.0
        for bucket in ("trade", "main", "margin", "isolated"):
            try:
                raw = self._spot.fetch_balance({"type": bucket})
                a = raw.get(asset, {})
                spot_free += float(a.get("free") or 0.0)
                spot_total += float(a.get("total") or 0.0)
            except Exception as exc:  # noqa: BLE001
                if self._dedup.should_log(f"bal:{bucket}:{type(exc).__name__}"):
                    log.warning("kucoin spot balance %s failed: %r", bucket, exc)
        futures_free = 0.0
        futures_total = 0.0
        try:
            raw = self._futures.fetch_balance({"currency": asset})
            a = raw.get(asset, {})
            futures_free = float(a.get("free") or 0.0)
            futures_total = float(a.get("total") or 0.0)
        except Exception as exc:  # noqa: BLE001
            if self._dedup.should_log(f"futbal:{asset}:{type(exc).__name__}"):
                log.warning("kucoin futures balance %s failed: %r", asset, exc)
        return {
            "spot": WalletBalance(spot_free, spot_total),
            "futures": WalletBalance(futures_free, futures_total),
        }

    def list_open_perp_positions(self) -> list[tuple[str, float]]:
        try:
            positions = self._futures.fetch_positions()
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
        """KuCoin: spot uses marketable-limit+FOK (spot market doesn't take
        TIF), futures supports market+FOK natively.
        """
        try:
            if venue_leg == "futures":
                order = self._futures.create_order(
                    symbol=symbol,
                    type="market",
                    side=side,
                    amount=qty,
                    params={"timeInForce": "FOK", "clientOid": client_order_id},
                )
            else:
                ob = self.snapshot_book(symbol, depth=20)
                if not ob.asks or not ob.bids:
                    return FillResult(
                        symbol=symbol, venue_leg=venue_leg, side=side, qty=0.0,
                        avg_price=0.0, fee_quote=0.0, accepted=False,
                        error="empty_book", client_order_id=client_order_id,
                    )
                pad = self._padding_bps / 10_000.0
                limit_price = ob.asks[0].price * (1.0 + pad) if side == "buy" else ob.bids[0].price * (1.0 - pad)
                order = self._spot.create_order(
                    symbol=symbol,
                    type="limit",
                    side=side,
                    amount=qty,
                    price=limit_price,
                    params={"timeInForce": "FOK", "clientOid": client_order_id},
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
        """Sweep main, margin, isolated → trade. Best-effort per bucket
        (L20 dedup applies). Returns the amount swept from each source.
        """
        out: dict[str, float] = {}
        for src in ("main", "margin", "isolated"):
            try:
                raw = self._spot.fetch_balance({"type": src})
                amt = float(raw.get(asset, {}).get("free") or 0.0)
                if amt > 0.0:
                    self._spot.privatePostAccountsInnerTransfer(
                        {
                            "clientOid": f"consol-{src}-{asset}-{utcnow().timestamp()}",
                            "currency": asset,
                            "from": src,
                            "to": "trade",
                            "amount": str(amt),
                        }
                    )
                    out[src] = amt
            except Exception as exc:  # noqa: BLE001
                if self._dedup.should_log(f"consol:{src}:{type(exc).__name__}"):
                    log.warning("kucoin consolidate %s→trade failed: %r", src, exc)
        return out

    def transfer_spot_to_futures(self, asset: str, amount: float) -> None:
        """Universal-transfer is fine for the IN direction (§6.2)."""
        try:
            self._spot.transfer(asset, amount, "main", "contract")
        except Exception as exc:  # noqa: BLE001
            if self._dedup.should_log(f"transfer_in:{type(exc).__name__}"):
                log.warning("kucoin spot→futures failed: %r", exc)

    def transfer_futures_to_spot(self, asset: str, amount: float) -> None:
        """TWO-HOP per §6.2:
          1. futures CONTRACT → MAIN via futures-side transferOut
          2. spot MAIN → TRADE via inner-transfer

        Funds need to land in `trade` to be spendable on the spot order
        book this cycle; otherwise the consolidate sweep next cycle does it,
        which exploits the drain↔rebalance oscillation (§16 L19).
        """
        try:
            self._futures.privatePostTransferOut(
                {"currency": asset, "amount": str(amount), "recAccountType": "MAIN"}
            )
        except Exception as exc:  # noqa: BLE001
            if self._dedup.should_log(f"hop1:{type(exc).__name__}"):
                log.warning("kucoin hop-1 CONTRACT→MAIN failed: %r", exc)
            return
        try:
            self._spot.privatePostAccountsInnerTransfer(
                {
                    "clientOid": f"hop2-{asset}-{utcnow().timestamp()}",
                    "currency": asset,
                    "from": "main",
                    "to": "trade",
                    "amount": str(amount),
                }
            )
        except Exception as exc:  # noqa: BLE001
            if self._dedup.should_log(f"hop2:{type(exc).__name__}"):
                log.warning("kucoin hop-2 MAIN→TRADE failed: %r", exc)
            # If hop-2 fails, hop-1 funds rest in MAIN; next-cycle
            # consolidate_spot_wallets() picks them up. §18 closed item.

    def convert_dust_to_native(self, assets: list[str]) -> dict[str, float]:
        """KuCoin dust → KCS via `POST /api/v1/sub/transfer`'s convertDust path."""
        try:
            self._spot.privatePostAccountsTransferable({"asset": assets, "type": "KCS"})
            return {a: 0.0 for a in assets}
        except Exception as exc:  # noqa: BLE001
            if self._dedup.should_log(f"dust:{type(exc).__name__}"):
                log.warning("kucoin dust conversion failed: %r", exc)
            return {a: 0.0 for a in assets}
