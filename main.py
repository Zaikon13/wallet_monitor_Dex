# main.py
# Cronos DeFi Sentinel — drop-in aligned to your repo
# Clean imports only; no non-existent symbols.

import os
import time
import logging
from decimal import Decimal, getcontext
from dotenv import load_dotenv

# ---- Internal imports (that exist in your repo) ----
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
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)) or default)
    except Exception:
        return default

def _env_list(key: str) -> list[str]:
    raw = os.getenv(key, "") or ""
    return [x.strip() for x in raw.split(",") if x.strip()]

# ---- Main loop ----
def run():
    log.info("🟢 Starting Cronos DeFi Sentinel")

    WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
    RPC = os.getenv("RPC", "")
    DEX_PAIRS = _env_list("DEX_PAIRS")
    PRICE_MOVE_THRESHOLD = _env_float("PRICE_MOVE_THRESHOLD", 0.0)
    INTRADAY_HOURS = _env_float("INTRADAY_HOURS", 1.0)

    try:
        while True:
            # Snapshot wallet (core.holdings)
            try:
                # Support both signatures: (address) or (address, rpc)
                try:
                    snapshot = get_wallet_snapshot(WALLET_ADDRESS, RPC)
                except TypeError:
                    snapshot = get_wallet_snapshot(WALLET_ADDRESS)
                log.info("Snapshot taken.")
            except Exception:
                log.exception("Failed to snapshot wallet")
                snapshot = {"assets": [], "timestamp": None}

            # Build + send report
            try:
                report = build_day_report_text(snapshot)
                send_telegram(report)
            except Exception:
                log.exception("Failed to build/send report")

            # Pair alerts
            try:
                for pair in DEX_PAIRS:
                    alert = check_pair_alert(pair, PRICE_MOVE_THRESHOLD)
                    if alert:
                        send_telegram(alert)
            except Exception:
                log.exception("Pair alerts scan failed")

            # Sleep cadence
            sleep_s = max(60, int(INTRADAY_HOURS * 3600))
            time.sleep(sleep_s)

    except KeyboardInterrupt:
        log.warning("🛑 Keyboard interrupt received. Shutting down.")
    except Exception:
        log.exception("Unexpected error in main loop")

if __name__ == "__main__":
    run()
