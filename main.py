# FILE: main.py
# Purpose: Cronos DeFi Sentinel — stable, modular entrypoint.
# Notes:
#   - Imports are normalized to match the current repository modules.
#   - Uses get_wallet_snapshot (core.holdings) instead of non-existent snapshot_wallet.
#   - Uses send_telegram / telegram_long_poll_loop (telegram.api).
#   - Uses safe_get/safe_json (utils.http).
#   - Scans DEX_PAIRS with check_pair_alert (core.alerts).

import os
import time
import signal
import logging
from decimal import getcontext
from threading import Thread

from dotenv import load_dotenv

from core.config import (
    DATA_DIR,
    LOCK,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    WALLET_ADDRESS,
    RPC,
    ETHERSCAN_API,
    DISCOVER_ENABLED,
    TZ,
    INTRADAY_HOURS,
    DEX_PAIRS,
    PRICE_MOVE_THRESHOLD,
    GUARD_WINDOW_HRS,
)

from core.holdings import get_wallet_snapshot
from core.alerts import check_pair_alert
from reports.day_report import build_day_report_text
from telegram.api import send_telegram, telegram_long_poll_loop
from utils.http import safe_get, safe_json

# -----------------------------------------------------------------------------
# Global setup
# -----------------------------------------------------------------------------
getcontext().prec = 28
load_dotenv()

log = logging.getLogger("main")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

_running = True


def _signal_handler(signum, frame):
    global _running
    log.warning("Signal %s received. Shutting down gracefully...", signum)
    _running = False


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# -----------------------------------------------------------------------------
# Optional: Telegram long-poll loop (commands/ops)
# -----------------------------------------------------------------------------
def _start_telegram_long_polling():
    try:
        # Run the bot long-poll loop in the background (non-blocking).
        telegram_long_poll_loop()
    except Exception as e:
        log.exception("Telegram long-poll loop terminated with error: %s", e)


# -----------------------------------------------------------------------------
# Main intraday loop
# -----------------------------------------------------------------------------
def run():
    log.info("🟢 Starting Cronos DeFi Sentinel")

    # Start Telegram long-polling in background (optional, non-fatal if it fails)
    try:
        t = Thread(target=_start_telegram_long_polling, name="telegram-long-poll", daemon=True)
        t.start()
    except Exception as e:
        log.exception("Failed to start telegram long-poll thread: %s", e)

    try:
        while _running:
            with LOCK:
                # Snapshot holdings
                snapshot = get_wallet_snapshot(WALLET_ADDRESS, RPC)
                log.info("Snapshot taken at %s", snapshot.get("timestamp", "N/A"))

                # Day report
                report = build_day_report_text(snapshot)
                send_telegram(report)

                # DEX pair alerts
                for pair in DEX_PAIRS:
                    try:
                        alert_msg = check_pair_alert(pair, PRICE_MOVE_THRESHOLD)
                        if alert_msg:
                            send_telegram(alert_msg)
                    except Exception as e:
                        log.exception("Alert check failed for %s: %s", pair, e)

            # Sleep until next intraday tick
            sleep_s = max(60, int(INTRADAY_HOURS * 3600))
            log.info("Sleeping for %s seconds...", sleep_s)
            for _ in range(sleep_s):
                if not _running:
                    break
                time.sleep(1)

    except Exception as e:
        log.exception("Unexpected error in main loop: %s", e)
    finally:
        log.info("🛑 Cronos DeFi Sentinel stopped.")


if __name__ == "__main__":
    run()
