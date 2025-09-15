# core/config.py
import os
from dataclasses import dataclass

# Legacy aliases → new envs
_ALIAS = {
    "ALERTS_INTERVAL_MINUTES": "ALERTS_INTERVAL_MIN",
    "DISCOVER_REQUIRE_WCRO_QUOTE": "DISCOVER_REQUIRE_WCRO",
}
for src, dst in _ALIAS.items():
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)

@dataclass(frozen=True)
class Settings:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    WALLET_ADDRESS: str = (os.getenv("WALLET_ADDRESS", "")).lower()
    ETHERSCAN_API: str = os.getenv("ETHERSCAN_API", "")
    CRONOS_RPC_URL: str = os.getenv("CRONOS_RPC_URL", "")

    # Discovery / watch rules (χρησιμοποιούνται αργότερα)
    PRICE_MOVE_THRESHOLD: float = float(os.getenv("PRICE_MOVE_THRESHOLD","5"))
    ALERTS_INTERVAL_MIN: int = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
    DISCOVER_MIN_LIQ_USD: float = float(os.getenv("DISCOVER_MIN_LIQ_USD","30000"))
    DISCOVER_MIN_VOL24_USD: float = float(os.getenv("DISCOVER_MIN_VOL24_USD","5000"))
    DISCOVER_REQUIRE_WCRO: bool = os.getenv("DISCOVER_REQUIRE_WCRO","false").lower() in ("1","true","yes","on")

    WALLET_POLL: int = int(os.getenv("WALLET_POLL","15"))
    INTRADAY_HOURS: int = int(os.getenv("INTRADAY_HOURS","3"))
    EOD_HOUR: int = int(os.getenv("EOD_HOUR","23"))
    EOD_MINUTE: int = int(os.getenv("EOD_MINUTE","59"))

    DATA_DIR: str = os.getenv("DATA_DIR", "/app/data")

settings = Settings()
