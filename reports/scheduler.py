"""Helpers for scheduling end-of-day reports and background loops."""
from __future__ import annotations

import logging
import threading
import time

import schedule

from reports.day_report import build_day_report_text
from telegram.api import send_telegram

log = logging.getLogger(__name__)


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
            except Exception:  # pragma: no cover - defensive logging
                log.exception("Scheduler encountered an error")
                try:
                    send_telegram("⚠️ Scheduler error", escape=True)
                except Exception:
                    log.debug("Unable to notify scheduler error", exc_info=True)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def send_daily_report() -> None:
    """Compose and send the daily report through Telegram."""
    try:
        text = build_day_report_text()
    except Exception:
        log.exception("Failed to compose daily report")
        text = "Daily report unavailable (n/a)."
    send_telegram(text)


def schedule_daily_report(eod_time: str = "23:59") -> None:
    """Register the end-of-day report job."""
    schedule.every().day.at(eod_time).do(send_daily_report)
