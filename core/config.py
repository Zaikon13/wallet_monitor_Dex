import os
from typing import Dict

ENV_ALIASES: Dict[str, str] = {
    "BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "CHAT_ID": "TELEGRAM_CHAT_ID",
    "WALLET": "WALLET_ADDRESS",
    "RPC": "CRONOS_RPC_URL",
    "RPC_URL": "CRONOS_RPC_URL",
    "ETHERSCAN": "ETHERSCAN_API",
    "ALERTS_INTERVAL_MINUTES": "ALERTS_INTERVAL_MIN",
    "DISCOVER_REQUIRE_WCRO_QUOTE": "DISCOVER_REQUIRE_WCRO",
}

def apply_env_aliases() -> None:
    """
    Apply alias mapping: if an alias exists in env and the target is not set, copy it.
    """
    for short, full in ENV_ALIASES.items():
        val = os.getenv(short)
        if val and not os.getenv(full):
            os.environ[full] = val

def get_env(key: str, default: str | None = None) -> str | None:
    """Wrapper for os.getenv with default fallback."""
    return os.getenv(key, default)

def require_env(key: str) -> str:
    """Like get_env but throws if the environment variable is missing."""
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val
