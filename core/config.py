# -*- coding: utf-8 -*-
"""
core/config.py

Centralized configuration loader for Cronos DeFi Sentinel.
Replaces scattered os.getenv calls with typed getters and AppConfig dataclass.

Usage:
    from core.config import AppConfig
    config = AppConfig()
"""

import os
from dataclasses import dataclass
from typing import Optional


def get_str(key: str, default: Optional[str] = None) -> str:
    return os.getenv(key, default or "")


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, default))
    except Exception:
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(key, default))
    except Exception:
        return default


def get_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AppConfig:
    # Telegram
    telegram_bot_token: str = get_str("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = get_str("TELEGRAM_CHAT_ID")

    # Wallet / APIs
    wallet_address: str = get_str("WALLET_ADDRESS").lower()
    etherscan_api: str = get_str("ETHERSCAN_API")

    # Polling / intervals
    alerts_interval_minutes: int = get_int("ALERTS_INTERVAL_MINUTES", 15)
    dex_poll: int = get_int("DEX_POLL", 60)
    wallet_poll: int = get_int("WALLET_POLL", 15)

    # Discovery
    discover_enabled: bool = get_bool("DISCOVER_ENABLED", True)
    discover_limit: int = get_int("DISCOVER_LIMIT", 10)
    discover_query: str = get_str("DISCOVER_QUERY", "cronos")
    discover_max_pair_age_hours: int = get_int("DISCOVER_MAX_PAIR_AGE_HOURS", 24)
    discover_min_liq_usd: float = get_float("DISCOVER_MIN_LIQ_USD", 30000)
    discover_min_vol24_usd: float = get_float("DISCOVER_MIN_VOL24_USD", 5000)

    # End of Day
    eod_hour: int = get_int("EOD_HOUR", 23)
    eod_minute: int = get_int("EOD_MINUTE", 59)

    # Thresholds / alerts
    price_move_threshold: float = get_float("PRICE_MOVE_THRESHOLD", 5.0)
    pump_alert_24h_pct: float = get_float("PUMP_ALERT_24H_PCT", 20.0)
    dump_alert_24h_pct: float = get_float("DUMP_ALERT_24H_PCT", -15.0)
    risky_pump_default: float = get_float("RISKY_PUMP_DEFAULT", 20.0)
    risky_dump_default: float = get_float("RISKY_DUMP_DEFAULT", -15.0)

    # Guard window (anti-false positive)
    guard_window_min: int = get_int("GUARD_WINDOW_MIN", 60)
    guard_pump_pct: float = get_float("GUARD_PUMP_PCT", 20.0)
    guard_drop_pct: float = get_float("GUARD_DROP_PCT", -12.0)
    guard_trail_drop_pct: float = get_float("GUARD_TRAIL_DROP_PCT", -8.0)

    # Timezone
    tz: str = get_str("TZ", "Europe/Athens")

    # RPC
    cronos_rpc_url: str = get_str(
        "CRONOS_RPC_URL", "https://cronos-evm-rpc.publicnode.com"
    )


def load_config() -> AppConfig:
    """
    Factory for AppConfig (useful for tests or reloads).
    """
    return AppConfig()
