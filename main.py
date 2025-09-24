# main.py
# Cronos DeFi Sentinel — minimal, repo-aligned entrypoint
# This section covers: clean imports, boot loop, snapshot → report → alerts, Telegram send, graceful shutdown.
# Nothing else is added or changed beyond import/name normalization.

import os
import sys
import time
import json
import threading
import logging
import signal
import random
from collections import deque, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, getcontext

from dotenv import load_dotenv

# ---- Internal imports (aligned with repo) ----
from core.config import (
    DATA_DIR,
    LOCK,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WALLET_ADDRESS,
    RPC,
    TZ,
    INTRADAY_HOURS,
    DEX_PAIRS,
    PRICE_MOVE_THRESHOLD,
)
from core.rpc import snapshot_wallet
from core.holdings import get_wallet_snapshot
from core.alerts import check_pair_alert
from reports.day_report import build_day_report_text
from telegram.api import telegram_long_poll_loop, send_telegram
from utils.http import safe_get, safe_json

# ---- Precision / context settings ----
getcontext().prec = 28
load_dotenv()

# ---- Logging ----
log = logging.getLogger("main")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

# ---- Main loop ----
def run():
    log.info("🟢 Starting Cronos DeFi Sentinel")

    try:
        while True:
            with LOCK:
                # Snapshot (RPC state of wallet)
                snapshot = snapshot_wallet(WALLET_ADDRESS, RPC)
                log.info("Snapshot taken at %s", snapshot.get("timestamp"))

                # Build & send daily-style text for current snapshot
                report = build_day_report_text(snapshot)
                try:
                    send_telegram(report)
                except Exception:
                    log.exception("Failed to send snapshot report to Telegram")

                # Pair alerts (per-pair check; only send if alert text is returned)
                try:
                    for pair in DEX_PAIRS:
                        alert = check_pair_alert(pair, PRICE_MOVE_THRESHOLD)
                        if alert:
                            send_telegram(alert)
                except Exception:
                    log.exception("Pair alerts scan failed")

            # Sleep for configured intraday cadence
            try:
                sleep_s = max(60, int(INTRADAY_HOURS * 3600))
            except Exception:
                sleep_s = 3600
            time.sleep(sleep_s)

    except KeyboardInterrupt:
        log.warning("🛑 Keyboard interrupt received. Shutting down.")
    except Exception:
        log.exception("Unexpected error in main loop")


if __name__ == "__main__":
    run()
