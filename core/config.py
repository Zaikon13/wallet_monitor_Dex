# core/config.py

from __future__ import annotations
import os
import logging
from typing import Dict, Tuple

# Optional short→full env aliases, so older names still work
ENV_ALIASES: Dict[str, str] = {
    "BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "CHAT_ID": "TELEGRAM_CHAT_ID",
    "WALLET": "WALLET_ADDRESS",
    "RPC": "CRONOS_RPC_URL",
    "ETHERSCAN": "ETHERSCAN_API",
}

def apply_env_aliases() -> None:
    """Copy values from legacy short envs to the expected full names if missing."""
    for short, full in ENV_ALIASES.items():
        val = os.getenv(short)
        if val and not os.getenv(full):
            os.environ[full] = val

def get_env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key, default)

# ---- Validation ----

REQUIRED_ENV: Tuple[str, ...] = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "WALLET_ADDRESS",
    "CRONOS_RPC_URL",
)

def validate_env(strict: bool = False) -> Dict[str, str]:
    """
    Validate required env vars. If strict=True, raise on missing; otherwise log warnings.
    Returns a dict of present values.
    """
    present: Dict[str, str] = {}
    missing = []
    for key in REQUIRED_ENV:
        val = (os.getenv(key) or "").strip()
        if val:
            present[key] = val
        else:
            missing.append(key)

    if missing:
        msg = f"Missing required env vars: {', '.join(missing)}"
        if strict:
            raise ValueError(msg)
        logging.warning(msg)

    return present
