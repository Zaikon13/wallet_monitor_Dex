# core/config.py
# -*- coding: utf-8 -*-
"""
Centralized config: reads env + applies a few aliases so main.py can import constants safely.
"""

import os

# ---- Aliases (so Railway / other env names still work) ----
def _alias_env(src: str, dst: str):
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)

# Common aliases you said you use on Railway
_alias_env("ALERTS_INTERVAL_MINUTES", "ALERTS_INTERVAL_MIN")
_alias_env("DISCOVER_REQUIRE_WCRO_QUOTE", "DISCOVER_REQUIRE_WCRO")
_alias_env("BOT_TOKEN", "TELEGRAM_BOT_TOKEN")
_alias_env("CHAT_ID", "TELEGRAM_CHAT_ID")
_alias_env("WALLET", "WALLET_ADDRESS")
_alias_env("RPC", "CRONOS_RPC_URL")
_alias_env("ETHERSCAN", "ETHERSCAN_API")

# ---- Telegram ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ---- Wallet / RPC ----
WALLET_ADDRESS = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API  = os.getenv("ETHERSCAN_API", "")
CRONOS_RPC_URL = os.getenv("CRONOS_RPC_URL", "")

# ---- Dex Pairs (as list) ----
_raw_pairs = os.getenv("DEX_PAIRS", "")
DEX_PAIRS = [p.strip() for p in _raw_pairs.split(",") if p.strip()]

# ---- Polling (main uses os.getenv directly for WALLET_POLL, but keep here too) ----
WALLET_POLL = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL    = int(os.getenv("DEX_POLL", "60"))

# ---- Alerts thresholds ----
ALERTS_INTERVAL_MIN = int(os.getenv("ALERTS_INTERVAL_MIN", "15"))
DUMP_ALERT_24H_PCT  = float(os.getenv("DUMP_ALERT_24H_PCT", "-15"))
PUMP_ALERT_24H_PCT  = float(os.getenv("PUMP_ALERT_24H_PCT", "20"))

# ---- Guard window ----
GUARD_WINDOW_MIN     = int(os.getenv("GUARD_WINDOW_MIN", "60"))
GUARD_PUMP_PCT       = float(os.getenv("GUARD_PUMP_PCT", "20"))
GUARD_DROP_PCT       = float(os.getenv("GUARD_DROP_PCT", "-12"))
GUARD_TRAIL_DROP_PCT = float(os.getenv("GUARD_TRAIL_DROP_PCT", "-8"))

# ---- Discovery (Dexscreener auto-discovery) ----
DISCOVER_ENABLED              = os.getenv("DISCOVER_ENABLED", "false").lower() in ("1","true","yes","on")
DISCOVER_QUERY                = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT                = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL                 = int(os.getenv("DISCOVER_POLL", "120"))
DISCOVER_MIN_LIQ_USD          = float(os.getenv("DISCOVER_MIN_LIQ_USD", "30000"))
DISCOVER_MIN_VOL24_USD        = float(os.getenv("DISCOVER_MIN_VOL24_USD", "5000"))
DISCOVER_MIN_ABS_CHANGE_PCT   = float(os.getenv("DISCOVER_MIN_ABS_CHANGE_PCT", "10"))
DISCOVER_MAX_PAIR_AGE_HOURS   = int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS", "24"))
DISCOVER_REQUIRE_WCRO         = os.getenv("DISCOVER_REQUIRE_WCRO", "false").lower() in ("1","true","yes","on")

# ---- Reports / TZ ----
TZ             = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR       = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE     = int(os.getenv("EOD_MINUTE", "59"))

# ---- Data dir ----
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
