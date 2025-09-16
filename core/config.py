# core/config.py
# Environment configuration + aliases

from __future__ import annotations
import os
from typing import Dict

# Χάρτης aliases ώστε main.py να βρίσκει πάντα τις σωστές μεταβλητές
ENV_ALIASES: Dict[str, str] = {
    "BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "CHAT_ID": "TELEGRAM_CHAT_ID",
    "WALLET": "WALLET_ADDRESS",
    "RPC": "RPC_URL",
    "ETHERSCAN": "ETHERSCAN_API",
}

def apply_env_aliases() -> None:
    """
    Εφαρμόζει alias mapping:
    Αν υπάρχει π.χ. BOT_TOKEN στο env αλλά όχι TELEGRAM_BOT_TOKEN,
    θα το αντιγράψει εκεί.
    """
    for short, full in ENV_ALIASES.items():
        val = os.getenv(short)
        if val and not os.getenv(full):
            os.environ[full] = val

def get_env(key: str, default: str | None = None) -> str | None:
    """Wrapper για os.getenv με fallback default."""
    return os.getenv(key, default)

def require_env(key: str) -> str:
    """Όπως get_env αλλά σπάει αν λείπει η μεταβλητή."""
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val
