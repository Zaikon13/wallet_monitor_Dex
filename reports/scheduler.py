"""Helpers for scheduling end-of-day reports and background loops."""
from __future__ import annotations

import logging
import threading
import time

import schedule

from reports.day_report import build_day_report_text
from telegram.api import send_telegram_message


def run_pending() -> None:
    """Run any pending scheduled jobs once."""
    schedule.run_pending()


def start_eod_scheduler() -> None:
    """Forever loop that runs pending jobs every minute."""
    while True:
        schedule.run_pending()
        time.sleep(60)


def run_scheduler() -> None:
    """Background thread that polls the scheduler every second."""
    def _run() -> None:
        while True:
            try:
                run_pending()
                time.sleep(1)
            except Exception:
                logging.exception("Scheduler encountered an error")
                send_telegram_message("⚠️ Scheduler error")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def schedule_daily_report(eod_time: str = "23:59") -> None:
    schedule.every().day.at(eod_time).do(send_daily_report)


def send_daily_report() -> None:
    try:
        text = build_day_report_text()
        send_telegram_message(f"📒 Daily Report\n{text}")
    except Exception:
        logging.exception("Failed to build or send daily report")
        send_telegram_message("⚠️ Failed to generate daily report.")
