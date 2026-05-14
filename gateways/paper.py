"""Synthetic gateway for paper mode AND deterministic unit tests.

Paper-mode fills are computed against the most-recent book snapshot the
caller injected. The gateway is `InMemoryGateway` — purely deterministic,
no network. Live mode uses the real venue gateway; paper-cycle tests
use this one.

The synthetic fee + slippage model is driven by `MergedConfig`'s paper_*
fields (§4 globals).
"""

from __future__ import annotations

from datetime import timedelta
from typing import cast

from core.types import (
    BookLevel,
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


class InMemoryGateway:
    """In-memory deterministic gateway. Books, funding rates, balances are
    set explicitly by the test or paper-mode harness.

    Order placement applies the configured synthetic slippage + fee, then
    debits/credits the in-memory wallet so subsequent calls see updated state.
    """

    def __init__(
        self,
        exchange_id: ExchangeId = "binance",
        slippage_bps: float = 5.0,
        fee_bps: float = 4.0,
        expected_account_id: str = "paper-account",
        track_wallet_drawdown: bool = True,
    ) -> None:
        self._exchange_id: ExchangeId = exchange_id
        self.slippage_bps = slippage_bps
        self.fee_bps = fee_bps
        self._expected_account_id = expected_account_id
        self.track_wallet_drawdown = track_wallet_drawdown

        # State the test/harness mutates directly:
        self.books: dict[str, BookSnapshot] = {}
        self.funding: dict[str, FundingInfo] = {}
        self.fees: dict[tuple[str, str], FeeInfo] = {}
        self.balances: dict[VenueLeg, dict[str, WalletBalance]] = {
            "spot": {},
            "futures": {},
        }
        self.markets_loaded = False
        self.ticks: dict[str, float] = {}
        self.lots: dict[str, float] = {}
        self.min_notionals: dict[str, float] = {}
        self.open_perps: list[tuple[str, float]] = []
        # Used by the §3.1 mitigation policy idempotency check (§16 L43):
        self.seen_client_order_ids: set[str] = set()

    # ─── identity ──────────────────────────────────────────────────────
    @property
    def exchange_id(self) -> ExchangeId:
        return self._exchange_id

    def expected_account_id(self) -> str:
        return self._expected_account_id

    def actual_account_id(self) -> str:
        return self._expected_account_id

    def probe_permissions(self) -> dict[str, bool]:
        return {"spot_read": True, "futures_read": True, "spot_trade": True, "futures_trade": True}

    def account_mode_probe(self) -> str:
        return "unified"

    # ─── markets ───────────────────────────────────────────────────────
    def load_markets(self, force: bool = False) -> None:
        self.markets_loaded = True

    def tick_size(self, symbol: str) -> float:
        return self.ticks.get(symbol, 0.01)

    def lot_step(self, symbol: str) -> float:
        return self.lots.get(symbol, 0.001)

    def min_notional(self, symbol: str) -> float:
        return self.min_notionals.get(symbol, 5.0)

    # ─── data ──────────────────────────────────────────────────────────
    def fetch_funding_rates(self) -> list[FundingInfo]:
        return list(self.funding.values())

    def fetch_predicted_funding(self, perp_symbol: str) -> FundingInfo:
        if perp_symbol not in self.funding:
            raise KeyError(f"No funding fixture for {perp_symbol}")
        return self.funding[perp_symbol]

    def snapshot_book(self, symbol: str, depth: int = 20) -> BookSnapshot:
        if symbol not in self.books:
            raise KeyError(f"No book fixture for {symbol}")
        return self.books[symbol]

    def fetch_fees(self, spot_symbol: str, perp_symbol: str) -> FeeInfo:
        key = (spot_symbol, perp_symbol)
        return self.fees.get(key, FeeInfo(spot_fee_bps=self.fee_bps, perp_fee_bps=self.fee_bps))

    def fetch_balance(self, asset: str) -> dict[VenueLeg, WalletBalance]:
        return {
            "spot": self.balances["spot"].get(asset, WalletBalance(0.0, 0.0)),
            "futures": self.balances["futures"].get(asset, WalletBalance(0.0, 0.0)),
        }

    def list_open_perp_positions(self) -> list[tuple[str, float]]:
        return list(self.open_perps)

    # ─── orders ────────────────────────────────────────────────────────
    def place_market_fok(
        self,
        symbol: str,
        venue_leg: VenueLeg,
        side: Side,
        qty: float,
        client_order_id: str,
    ) -> FillResult:
        if client_order_id in self.seen_client_order_ids:
            # L43: deterministic id → idempotent retry. Treat as duplicate-noop.
            return FillResult(
                symbol=symbol,
                venue_leg=venue_leg,
                side=side,
                qty=0.0,
                avg_price=0.0,
                fee_quote=0.0,
                accepted=False,
                error="duplicate_client_order_id",
                client_order_id=client_order_id,
            )
        self.seen_client_order_ids.add(client_order_id)

        if symbol not in self.books:
            return FillResult(
                symbol=symbol,
                venue_leg=venue_leg,
                side=side,
                qty=0.0,
                avg_price=0.0,
                fee_quote=0.0,
                accepted=False,
                error="no_book",
                client_order_id=client_order_id,
            )

        book = self.books[symbol]
        levels = book.asks if side == "buy" else book.bids

        # FOK: either the book has full depth at submission time, or reject.
        total_depth = sum(lvl.depth for lvl in levels)
        if qty > total_depth or qty <= 0.0:
            return FillResult(
                symbol=symbol,
                venue_leg=venue_leg,
                side=side,
                qty=0.0,
                avg_price=0.0,
                fee_quote=0.0,
                accepted=False,
                error="fok_rejected",
                client_order_id=client_order_id,
            )

        # vwap fill across consumed depth, then apply synthetic slippage in
        # the adverse direction.
        remaining = qty
        spent = 0.0
        for lvl in levels:
            if remaining <= 0.0:
                break
            take = min(lvl.depth, remaining)
            spent += take * lvl.price
            remaining -= take
        avg = spent / qty
        slip_mult = 1.0 + (self.slippage_bps / 10_000.0) * (1.0 if side == "buy" else -1.0)
        avg_after_slip = avg * slip_mult

        fee = qty * avg_after_slip * (self.fee_bps / 10_000.0)

        # Paper-mode wallet drawdown so subsequent cycles see honest balances.
        # We don't ledger fee-token credits separately; fees are debited from
        # the quote balance to model net cash impact.
        if self.track_wallet_drawdown and "/" in symbol:
            base, quote = symbol.split("/")[0], symbol.split("/")[1].split(":")[0]
            self._apply_wallet_delta(
                venue_leg=venue_leg,
                base=base,
                quote=quote,
                side=side,
                qty=qty,
                avg_price=avg_after_slip,
                fee_quote=fee,
            )

        return FillResult(
            symbol=symbol,
            venue_leg=venue_leg,
            side=side,
            qty=qty,
            avg_price=avg_after_slip,
            fee_quote=fee,
            accepted=True,
            error=None,
            client_order_id=client_order_id,
        )

    # ─── wallets ───────────────────────────────────────────────────────
    def _apply_wallet_delta(
        self,
        *,
        venue_leg: VenueLeg,
        base: str,
        quote: str,
        side: Side,
        qty: float,
        avg_price: float,
        fee_quote: float,
    ) -> None:
        bucket = self.balances[venue_leg]
        # Quote-currency delta:
        notional = qty * avg_price
        if side == "buy":
            self._mutate_balance(bucket, quote, free_delta=-(notional + fee_quote))
            if venue_leg == "spot":
                self._mutate_balance(bucket, base, free_delta=+qty)
        else:  # sell
            self._mutate_balance(bucket, quote, free_delta=+(notional - fee_quote))
            if venue_leg == "spot":
                self._mutate_balance(bucket, base, free_delta=-qty)

    @staticmethod
    def _mutate_balance(
        bucket: dict[str, WalletBalance], asset: str, *, free_delta: float
    ) -> None:
        cur = bucket.get(asset, WalletBalance(0.0, 0.0))
        bucket[asset] = WalletBalance(
            free=max(0.0, cur.free + free_delta),
            total=max(0.0, cur.total + free_delta),
        )

    def consolidate_spot_wallets(self, asset: str) -> dict[str, float]:
        return {}

    def transfer_spot_to_futures(self, asset: str, amount: float) -> None:
        return None

    def transfer_futures_to_spot(self, asset: str, amount: float) -> None:
        return None

    def convert_dust_to_native(self, assets: list[str]) -> dict[str, float]:
        return {a: 0.0 for a in assets}


def _book(
    symbol: str,
    mid: float,
    *,
    ask_levels: list[tuple[float, float]],
    bid_levels: list[tuple[float, float]],
    skew_ms: int = 0,
) -> BookSnapshot:
    """Test helper to construct BookSnapshot tersely."""
    asks = [BookLevel(price=p, depth=d) for p, d in ask_levels]
    bids = [BookLevel(price=p, depth=d) for p, d in bid_levels]
    ts = utcnow() + timedelta(milliseconds=skew_ms)
    return BookSnapshot(symbol=symbol, bids=bids, asks=asks, mid_price=mid, taken_at=ts)


__all__ = ["InMemoryGateway", "_book"]
