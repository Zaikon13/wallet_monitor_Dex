from __future__ import annotations
import os
import logging
from typing import Dict, List, Tuple

# Short → canonical env names
ENV_ALIASES: Dict[str, str] = {
    "BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "CHAT_ID": "TELEGRAM_CHAT_ID",
    "WALLET": "WALLET_ADDRESS",
    "RPC": "CRONOS_RPC_URL",
    "ETHERSCAN": "ETHERSCAN_API",
}

# Keys that should normally be present for a healthy runtime
REQUIRED_KEYS: List[str] = [
    "TZ",
    "CRONOS_RPC_URL",
    "WALLET_ADDRESS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "EOD_HOUR",
    "EOD_MINUTE",
]

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

def _strip_if_needed(key: str, val: str) -> Tuple[str, bool]:
    new = val.strip()
    return new, (new != val)

def validate_env(strict: bool = False) -> Tuple[bool, list[str]]:
    """
    Validate critical env vars.
    - Trim leading/trailing spaces (common .env formatting issue) and warn
    - Check REQUIRED_KEYS presence
    Returns (ok, warnings). If strict, raises on missing keys.
    """
    warnings: list[str] = []

    # Trim accidental spaces and log warnings
    for k, v in list(os.environ.items()):
        if not isinstance(v, str):
            continue
        new, changed = _strip_if_needed(k, v)
        if changed:
            os.environ[k] = new
            msg = f"Stripped spaces around env value: {k}"
            logging.warning(msg)
            warnings.append(msg)

    # Required keys
    missing = [k for k in REQUIRED_KEYS if not os.getenv(k)]
    if missing:
        msg = f"Missing required env keys: {', '.join(missing)}"
        logging.warning(msg)
        warnings.append(msg)
        if strict:
            raise RuntimeError(msg)

    ok = not missing
    return ok, warnings

__all__ = [
    "ENV_ALIASES",
    "REQUIRED_KEYS",
    "apply_env_aliases",
    "get_env",
    "require_env",
    "validate_env",
]
