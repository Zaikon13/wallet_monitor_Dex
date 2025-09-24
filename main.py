#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, sys, time, logging, signal
from datetime import datetime
from decimal import getcontext
from typing import Optional

from dotenv import load_dotenv  # type: ignore
import schedule  # type: ignore

from core.config import apply_env_aliases, validate_env, get_env
from telegram.api import send_telegram_message
from reports.day_report import build_day_report_text

try:
    from core.holdings import get_wallet_snapshot  # type: ignore
except Exception:
    get_wallet_snapshot = None  # type: ignore
try:
    from telegram.formatters import format_holdings  # type: ignore
except Exception:
    format_holdings = None  # type: ignore

getcontext().prec = 28

def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None: return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def _get_eod_time() -> str:
    t = os.getenv("EOD_TIME", "23:59").strip()
    try:
        hh, mm = t.split(":")
        ih, im = int(hh), int(mm)
        if 0 <= ih <= 23 and 0 <= im <= 59:
            return f"{ih:02d}:{im:02d}"
    except Exception:
        pass
    logging.warning("Invalid EOD_TIME '%s', using 23:59", t)
    return "23:59"

def send_startup_ping() -> None:
    try:
        send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")
    except Exception as e:
        logging.exception("Telegram startup ping failed: %s", e)

def send_daily_report() -> None:
    try:
        text = build_day_report_text()
        send_telegram_message("📒 Daily Report\n" + text)
    except Exception as e:
        logging.exception("Failed to build/send daily report: %s", e)
        try: send_telegram_message("⚠️ Failed to generate daily report.")
        except Exception: pass

def send_holdings_snapshot() -> None:
    if get_wallet_snapshot is None or format_holdings is None:
        logging.info("Holdings snapshot skipped (modules missing).")
        return
    try:
        wallet = os.getenv("WALLET_ADDRESS", "").strip()
        try:
            snap = get_wallet_snapshot(wallet) if wallet else get_wallet_snapshot()  # type: ignore
        except TypeError:
            snap = get_wallet_snapshot()  # type: ignore
        try:
            txt = format_holdings(snap)  # type: ignore
        except TypeError:
            txt = format_holdings(snapshot=snap)  # type: ignore
        send_telegram_message("🧾 Holdings Snapshot\n" + (txt or "Empty"))
    except Exception as e:
        logging.exception("Holdings snapshot error: %s", e)
        try: send_telegram_message("⚠️ Failed to build holdings snapshot.")
        except Exception: pass

def _bind_schedules() -> None:
    eod = _get_eod_time()
    schedule.every().day.at(eod).do(send_daily_report)
    logging.info("Scheduled daily report at %s", eod)
    ih = os.getenv("INTRADAY_HOURS", "").strip()
    if ih.isdigit() and int(ih) > 0:
        schedule.every(int(ih)).hours.do(send_holdings_snapshot)
        logging.info("Scheduled holdings snapshot every %s hour(s)", ih)

def scheduler_run_loop(stop: list[bool]) -> None:
    while not stop[0]:
        try: schedule.run_pending()
        except Exception as e: logging.exception("schedule.run_pending failed: %s", e)
        time.sleep(1)

def _install_signal_handlers(stop: list[bool]) -> None:
    def _graceful(sig, frame):
        logging.info("Signal %s, stopping...", sig); stop[0] = True
        try: send_telegram_message("🛑 Cronos DeFi Sentinel stopping...")
        except Exception: pass
    try:
        signal.signal(signal.SIGINT, _graceful)
        signal.signal(signal.SIGTERM, _graceful)
    except Exception:
        pass

def main() -> int:
    setup_logging()
    logging.info("Starting Cronos DeFi Sentinel...")
    try: load_dotenv(override=False)
    except Exception: pass
    apply_env_aliases()
    validate_env(strict=False)

    send_startup_ping()
    if _env_bool("STARTUP_SNAPSHOT", True):
        send_holdings_snapshot()

    _bind_schedules()
    stop = [False]; _install_signal_handlers(stop)
    try:
        scheduler_run_loop(stop)
    except Exception as e:
        logging.exception("Fatal in main loop: %s", e)
        try: send_telegram_message("❌ Fatal error in main loop.")
        except Exception: pass
        return 1
    logging.info("Bye.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
