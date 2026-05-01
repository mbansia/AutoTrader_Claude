from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore', case_sensitive=False)

    binance_api_key: str = Field(default='')
    binance_api_secret: str = Field(default='')
    dashboard_user: str = Field(default='admin')
    dashboard_password: str = Field(default='change-me')


# Strategy/runtime defaults intentionally coded in source (not required as env vars)
DATABASE_URL = 'sqlite:///./bot.db'
ENTRY_FUNDING_THRESHOLD = 0.0002
EXIT_FUNDING_THRESHOLD = 0.00005
MAX_HOLD_HOURS = 72
MAX_OPEN_POSITIONS = 1
MAX_TRADES_PER_DAY = 8
MAX_POSITION_NOTIONAL = 10.0
MIN_SYMBOL_NOTIONAL = 5.0
MIN_24H_QUOTE_VOLUME = 100000.0
LOOP_SECONDS = 30
DEFAULT_PAPER_MODE = True
DEFAULT_MAINTENANCE_MODE = False


settings = Settings()
