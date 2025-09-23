from __future__ import annotations
import os
from typing import Dict

ENV_ALIASES: Dict[str, str] = {
    "BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "CHAT_ID": "TELEGRAM_CHAT_ID",
    "WALLET": "WALLET_ADDRESS",
    "RPC": "CRONOS_RPC_URL",
    "ETHERSCAN": "ETHERSCAN_API",
}


def apply_env_aliases() -> None:
    for short, full in ENV_ALIASES.items():
        val = os.getenv(short)
        if val and not os.getenv(full):
            os.environ[full] = val


def get_env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)


def require_env(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"Missing env: {key}")
    return v
