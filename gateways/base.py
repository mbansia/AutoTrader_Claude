"""Abstract Gateway protocol. Every venue gateway implements this surface.

Methods are split into 3 groups:
  - Read-only (snapshot book, fetch funding, fetch fees, fetch balances): run in
    both paper and live modes, hit the venue's actual API.
  - Mutating (place_order, transfer, swap, dust_convert): paper mode synthesizes,
    live mode hits the venue.
  - Probes (account_id, mode, permissions): boot-time + periodic checks per
    §3.1 mitigation policy.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.types import (
    BookSnapshot,
    ExchangeId,
    FeeInfo,
    FillResult,
    FundingInfo,
    Side,
    VenueLeg,
    WalletBalance,
)


@runtime_checkable
class Gateway(Protocol):
    """Venue-agnostic interface. Implementations: paper (synthetic),
    binance, kucoin, etc.

    Symbols are venue-shape strings (e.g. "BTC/USDT" for spot, "BTC/USDT:USDT"
    for perp). The gateway handles per-venue quirks (KuCoin two-hop, Binance PM
    routing, etc.) internally per §6.
    """

    @property
    def exchange_id(self) -> ExchangeId: ...

    # ─── account / permissions ─────────────────────────────────────────
    def expected_account_id(self) -> str: ...
    def actual_account_id(self) -> str: ...
    def probe_permissions(self) -> dict[str, bool]: ...
    def account_mode_probe(self) -> str: ...  # "unified" / "classic"

    # ─── markets / metadata ────────────────────────────────────────────
    def load_markets(self, force: bool = False) -> None: ...
    def tick_size(self, symbol: str) -> float: ...
    def lot_step(self, symbol: str) -> float: ...
    def min_notional(self, symbol: str) -> float: ...

    # ─── data ──────────────────────────────────────────────────────────
    def fetch_funding_rates(self) -> list[FundingInfo]: ...
    def fetch_predicted_funding(self, perp_symbol: str) -> FundingInfo: ...
    def snapshot_book(self, symbol: str, depth: int = 20) -> BookSnapshot: ...
    def fetch_fees(self, spot_symbol: str, perp_symbol: str) -> FeeInfo: ...
    def fetch_balance(self, asset: str) -> dict[VenueLeg, WalletBalance]: ...
    def list_open_perp_positions(self) -> list[tuple[str, float]]: ...

    # ─── orders ────────────────────────────────────────────────────────
    def place_market_fok(
        self,
        symbol: str,
        venue_leg: VenueLeg,
        side: Side,
        qty: float,
        client_order_id: str,
    ) -> FillResult: ...

    # ─── wallets ───────────────────────────────────────────────────────
    def consolidate_spot_wallets(self, asset: str) -> dict[str, float]: ...
    def transfer_spot_to_futures(self, asset: str, amount: float) -> None: ...
    def transfer_futures_to_spot(self, asset: str, amount: float) -> None: ...
    def convert_dust_to_native(self, assets: list[str]) -> dict[str, float]: ...

    # ─── capital flows ─────────────────────────────────────────────────
    # §7.5 external_id construction; idempotent ingest. Per-venue impl
    # walks deposits + withdrawals + (where applicable) sub-account
    # transfers. Returns a list of normalised dicts:
    #   { "ts": datetime, "amount_usdt": float (signed; out=negative),
    #     "flow_type": "deposit" | "withdrawal" | "transfer",
    #     "external_id": "<venue>:<flow_type>:<id>",
    #     "note": str }
    # Empty list when no history endpoint available or zero activity.
    def list_capital_flow_records(self, lookback_days: int = 30) -> list[dict]: ...
