# core/config.py
# Environment configuration + aliases

from __future__ import annotations
import os
import logging
from typing import Dict, List

# Map short aliases → canonical env names
ENV_ALIASES: Dict[str, str] = {
    "BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "CHAT_ID": "TELEGRAM_CHAT_ID",
    "WALLET": "WALLET_ADDRESS",
    "RPC": "CRONOS_RPC_URL",
    "ETHERSCAN": "ETHERSCAN_API",
}

REQUIRED_KEYS: List[str] = [
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "WALLET_ADDRESS",
    # RPC έχει default, αλλά καλύτερα να υπάρχει
    "CRONOS_RPC_URL",
]

def apply_env_aliases() -> None:
    """Copy any short alias value into the canonical env var if missing."""
    for short, full in ENV_ALIASES.items():
        val = os.getenv(short)
        if val and not os.getenv(full):
            os.environ[full] = val

def get_env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)

def validate_env(strict: bool = False) -> list[str]:
    """Return list of missing required env keys. If strict=True, raise."""
    missing = [k for k in REQUIRED_KEYS if not os.getenv(k)]
    if missing:
        logging.warning("Missing env keys: %s", ", ".join(missing))
        if strict:
            raise RuntimeError(f"Missing required env keys: {', '.join(missing)}")
    return missing

def require_env(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"Missing required env: {key}")
    return v
