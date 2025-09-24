# main.py
# Cronos DeFi Sentinel — minimal, repo-aligned entrypoint
# Clean imports only (no ghost symbols)

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

# ---- Internal imports (aligned with actual repo) ----
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
            # Snapshot (RPC state of wallet)
            snapshot = snapshot_wallet(os.getenv("WALLET_ADDRESS", ""), os.getenv("RPC", ""))
            log.info("Snapshot taken at %s", snapshot.get("timestamp"))

            # Build & send daily-style text for current snapshot
            report = build_day_report_text(snapshot)
            try:
                send_telegram(report)
            except Exception:
                log.exception("Failed to send snapshot report to Telegram")

            # Pair alerts (per-pair check; only send if alert text is returned)
            try:
                pairs = (os.getenv("DEX_PAIRS", "") or "").split(",")
                threshold = float(os.getenv("PRICE_MOVE_THRESHOLD", "0") or 0)
                for pair in pairs:
                    if not pair:
                        continue
                    alert = check_pair_alert(pair, threshold)
                    if alert:
                        send_telegram(alert)
            except Exception:
                log.exception("Pair alerts scan failed")

            # Sleep for configured intraday cadence
            try:
                sleep_s = max(60, int(float(os.getenv("INTRADAY_HOURS", "1")) * 3600))
            except Exception:
                sleep_s = 3600
            time.sleep(sleep_s)

    except KeyboardInterrupt:
        log.warning("🛑 Keyboard interrupt received. Shutting down.")
    except Exception:
        log.exception("Unexpected error in main loop")


if __name__ == "__main__":
    run()
