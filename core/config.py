"""Process-wide config: env vars + the gateway registry.

Environment variables (read once at import; documented in §2.2):
  BINANCE_API_KEY / BINANCE_API_SECRET
  KUCOIN_API_KEY  / KUCOIN_API_SECRET  / KUCOIN_PASSPHRASE
  BINANCE_EXPECTED_ACCOUNT_ID  (v1.4 — boot-time assertion)
  KUCOIN_EXPECTED_ACCOUNT_ID   (v1.4 — boot-time assertion)
  DASHBOARD_USER / DASHBOARD_PASSWORD  (Basic auth on every UI route)
  DIAGNOSTICS_TOKEN                    (`?token=` for /api/diagnostics)
  DATABASE_URL                         (SQLite default, see state/db.py)
  BOT_LOOP_SECONDS                     (override config.loop_seconds)
  PAPER_MODE_ONLY                      ("1" → never construct live gateways)

The gateway registry is `(mode, exchange_id) → Gateway`. Diagnostics +
monitoring routes read from it; the bot loop registers gateways on boot.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from gateways.base import Gateway

Mode = Literal["paper", "live"]
ExchangeId = Literal["binance", "kucoin", "hyperliquid"]


@dataclass(frozen=True)
class EnvConfig:
    binance_api_key: str = ""
    binance_api_secret: str = ""
    kucoin_api_key: str = ""
    kucoin_api_secret: str = ""
    kucoin_passphrase: str = ""
    hyperliquid_wallet_address: str = ""
    hyperliquid_private_key: str = ""
    binance_expected_account_id: str = ""
    kucoin_expected_account_id: str = ""
    hyperliquid_expected_account_id: str = ""
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    diagnostics_token: str = ""
    paper_mode_only: bool = False


def load_env() -> EnvConfig:
    return EnvConfig(
        binance_api_key=os.environ.get("BINANCE_API_KEY", ""),
        binance_api_secret=os.environ.get("BINANCE_API_SECRET", ""),
        kucoin_api_key=os.environ.get("KUCOIN_API_KEY", ""),
        kucoin_api_secret=os.environ.get("KUCOIN_API_SECRET", ""),
        # Accept both env var names. The legacy v1.3 deploy uses
        # KUCOIN_API_PASSPHRASE; if the operator already has it set we
        # honour it. KUCOIN_PASSPHRASE is the shorter v1.5 form.
        kucoin_passphrase=(
            os.environ.get("KUCOIN_API_PASSPHRASE")
            or os.environ.get("KUCOIN_PASSPHRASE", "")
        ),
        hyperliquid_wallet_address=os.environ.get("HYPERLIQUID_WALLET_ADDRESS", ""),
        hyperliquid_private_key=os.environ.get("HYPERLIQUID_PRIVATE_KEY", ""),
        binance_expected_account_id=os.environ.get("BINANCE_EXPECTED_ACCOUNT_ID", ""),
        kucoin_expected_account_id=os.environ.get("KUCOIN_EXPECTED_ACCOUNT_ID", ""),
        hyperliquid_expected_account_id=os.environ.get("HYPERLIQUID_EXPECTED_ACCOUNT_ID", ""),
        dashboard_user=os.environ.get("DASHBOARD_USER", "admin"),
        dashboard_password=os.environ.get("DASHBOARD_PASSWORD", ""),
        diagnostics_token=os.environ.get("DIAGNOSTICS_TOKEN", ""),
        paper_mode_only=os.environ.get("PAPER_MODE_ONLY", "") == "1",
    )


_registry: dict[tuple[Mode, ExchangeId], Gateway] = {}


def register_gateway(mode: Mode, exchange_id: ExchangeId, gateway: Gateway) -> None:
    _registry[(mode, exchange_id)] = gateway


def get_gateway(mode: Mode, exchange_id: ExchangeId) -> Gateway | None:
    return _registry.get((mode, exchange_id))


def list_gateways() -> list[tuple[Mode, ExchangeId, Gateway]]:
    return [(m, x, g) for (m, x), g in _registry.items()]


def clear_registry() -> None:
    _registry.clear()
