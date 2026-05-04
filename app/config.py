"""Application-level settings + module-level strategy defaults.

Settings are env-driven via Pydantic. Defaults are deliberately
unrestrictive so paper-mode "just works" out of the box; LIVE mode
requires real Binance credentials (and, optionally, a writable
DATABASE_URL pointing at a persistent volume).

The ``ENTRY_FUNDING_APR`` / ``EXIT_FUNDING_APR`` constants below are
the *initial* values written into a fresh ``StrategyConfig`` row on
first run. After that, the user's edits via the Configuration page
take precedence.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore', case_sensitive=False)

    binance_api_key: str = Field(default='')
    binance_api_secret: str = Field(default='')
    # KuCoin requires three secrets (key + secret + passphrase). All three
    # must be set for the future KuCoinGateway to be active. Until then the
    # Monitoring tab still shows the configured-vs-not state.
    kucoin_api_key: str = Field(default='')
    kucoin_api_secret: str = Field(default='')
    kucoin_api_passphrase: str = Field(default='')
    dashboard_user: str = Field(default='admin')
    dashboard_password: str = Field(default='change-me')
    database_url: str = Field(default='sqlite:///./bot.db')


# Strategy/runtime defaults — all funding-rate thresholds are annualized (APR, decimal).
ENTRY_FUNDING_APR = 0.20
EXIT_FUNDING_APR = 0.05
MAX_HOLD_HOURS = 72
MAX_OPEN_POSITIONS = 1
MAX_TRADES_PER_DAY = 8
MAX_POSITION_NOTIONAL = 10.0
MIN_SYMBOL_NOTIONAL = 5.0
MIN_24H_QUOTE_VOLUME = 100000.0
LOOP_SECONDS = 30
DEFAULT_PAPER_MODE = True
DEFAULT_MAINTENANCE_MODE = False
STOP_LOSS_PCT = -0.02
PAPER_SLIPPAGE_BPS = 5
PAPER_FEE_BPS = 4


settings = Settings()
