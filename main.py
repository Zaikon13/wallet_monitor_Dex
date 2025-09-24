import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

import schedule
from dotenv import load_dotenv

from core.config import apply_env_aliases, require_env
from core.holdings import get_wallet_snapshot
from core.runtime_state import note_tick, update_snapshot
from core.watch import make_from_env
from reports.day_report import build_day_report_text
from reports.scheduler import EOD_TIME, run_pending
from telegram.api import send_telegram_message, telegram_long_poll_loop
from telegram.dispatcher import dispatch

try:
    from core.signals.server import start_signals_server_if_enabled
except ModuleNotFoundError:
    def start_signals_server_if_enabled():
        logging.warning("signals server disabled: Flask missing")

_shutdown = False
_wallet_address: Optional[str] = None


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default) == "1"


def graceful_shutdown(signum=None, frame=None):
    global _shutdown
    logging.info(f"Shutting down due to signal {signum}")
    _shutdown = True


def start_watchdog():
    def loop():
        while not _shutdown:
            time.sleep(60)
            logging.info("🫀 Alive watchdog ping")
    t = threading.Thread(target=loop, daemon=True)
    t.start()


def send_startup_ping():
    send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")


def send_daily_report():
    try:
        text = build_day_report_text()
        send_telegram_message(f"📒 Daily Report\n{text}")
    except Exception as e:
        logging.exception("Failed to build or send daily report")
        send_telegram_message("⚠️ Failed to generate daily report.")


def main_loop():
    global _wallet_address
    logging.info("Starting Cronos DeFi Sentinel")
    if os.path.exists(".env"):
        load_dotenv(".env")
    apply_env_aliases()
    _wallet_address = require_env("WALLET_ADDRESS")

    start_signals_server_if_enabled()
    send_startup_ping()
    start_watchdog()

    logging.info(f"Monitoring wallet: {_wallet_address}")
    schedule.every().day.at(EOD_TIME).do(send_daily_report)

    monitor = make_wallet_monitor(_wallet_address)
    while not _shutdown:
        try:
            monitor.tick()
            note_tick()
            run_pending()
            time.sleep(15)
        except Exception as e:
            logging.exception("Tick failure")
            send_telegram_message(f"❌ Tick failure: {e}")
            time.sleep(15)


def cli_diag():
    snapshot = get_wallet_snapshot()
    print("\n==== WALLET SNAPSHOT ====")
    for sym, info in snapshot.items():
        print(f"{sym:8} {info['amount']:>15,.4f}   ≈ {info['usd_value']:>10,.2f} USD")


def cli_once():
    snapshot = update_snapshot()
    print("\n==== UPDATED SNAPSHOT ====")
    for sym, info in snapshot.items():
        print(f"{sym:8} {info['amount']:>15,.4f}   ≈ {info['usd_value']:>10,.2f} USD")
    print("\n==== DAILY REPORT ====")
    print(build_day_report_text())


def make_wallet_monitor(wallet_address: str):
    from core.providers.cronos import fetch_wallet_txs
    from core.wallet_monitor import make_wallet_monitor
    return make_wallet_monitor(wallet_address, fetch_wallet_txs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGINT, graceful_shutdown)
    signal.signal(signal.SIGTERM, graceful_shutdown)

    if len(sys.argv) > 1:
        match sys.argv[1]:
            case "--once":
                cli_once()
            case "--diag":
                cli_diag()
            case _:  # Unknown CLI arg
                print("Unknown argument")
                sys.exit(1)
    else:
        threading.Thread(target=telegram_long_poll_loop, daemon=True).start()
        dispatch()  # long-poll Telegram bot commands
        main_loop()
