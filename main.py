# main.py - Entry point for Cronos DeFi Wallet Monitor
# Compatible with full repo structure (as extracted)

from dotenv import load_dotenv
import logging
import os
import schedule
import time
from core.config import apply_env_aliases
from core.runtime_state import start_runtime_state
from core.wallet_monitor import start_wallet_monitor
from core.watch import start_price_watcher
from reports.scheduler import start_schedulers
from telegram.dispatcher import start_telegram_bot
from telegram.api import send_telegram

# Load .env variables
load_dotenv()
apply_env_aliases()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def main():
    logger.info("Starting Cronos DeFi Sentinel...")
    send_telegram("✅ Cronos DeFi Sentinel started and is online.")

    # Start each component
    start_runtime_state()
    start_wallet_monitor()
    start_price_watcher()
    start_telegram_bot()
    start_schedulers()

    logger.info("All systems initialized. Entering main loop.")

    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down gracefully.")
            send_telegram("🛑 Cronos DeFi Sentinel stopped manually.")
            break
        except Exception as e:
            logger.exception("Unexpected error in main loop")
            send_telegram("⚠️ Fatal error in Cronos Sentinel loop")
            break

if __name__ == "__main__":
    main()
