# core/config.py
# Load configuration from environment variables

import os

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Wallet
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API", "")
CRONOS_RPC_URL     = os.getenv("CRONOS_RPC_URL", "")

# Dex pairs
DEX_PAIRS          = os.getenv("DEX_PAIRS", "").split(",") if os.getenv("DEX_PAIRS") else []

# Polling intervals
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL           = int(os.getenv("DEX_POLL", "60"))

# Alerts
ALERTS_INTERVAL_MIN     = int(os.getenv("ALERTS_INTERVAL_MIN", "15"))
DUMP_ALERT_24H_PCT      = float(os.getenv("DUMP_ALERT_24H_PCT", "-15"))
PUMP_ALERT_24H_PCT      = float(os.getenv("PUMP_ALERT_24H_PCT", "20"))

# Guard
GUARD_WINDOW_MIN   = int(os.getenv("GUARD_WINDOW_MIN", "60"))
GUARD_PUMP_PCT     = float(os.getenv("GUARD_PUMP_PCT", "20"))
GUARD_DROP_PCT     = float(os.getenv("GUARD_DROP_PCT", "-12"))
GUARD_TRAIL_DROP_PCT = float(os.getenv("GUARD_TRAIL_DROP_PCT", "-8"))

# Discovery
DISCOVER_ENABLED   = os.getenv("DISCOVER_ENABLED", "false").lower() == "true"
DISCOVER_QUERY     = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT     = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL      = int(os.getenv("DISCOVER_POLL", "120"))
DISCOVER_MIN_LIQ_USD    = float(os.getenv("DISCOVER_MIN_LIQ_USD", "30000"))
DISCOVER_MIN_VOL24_USD  = float(os.getenv("DISCOVER_MIN_VOL24_USD", "5000"))
DISCOVER_MIN_ABS_CHANGE_PCT = float(os.getenv("DISCOVER_MIN_ABS_CHANGE_PCT", "10"))
DISCOVER_MAX_PAIR_AGE_HOURS = int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS", "24"))
DISCOVER_REQUIRE_WCRO  = os.getenv("DISCOVER_REQUIRE_WCRO", "false").lower() == "true"

# Reports
TZ              = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS  = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR        = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE      = int(os.getenv("EOD_MINUTE", "59"))

# Data
DATA_DIR        = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
