import schedule
import time
import threading
import logging
from datetime import datetime
from reports.day_report import build_day_report_text
from telegram.api import send_telegram_message


def run_scheduler():
    def _run():
        while True:
            try:
                schedule.run_pending()
                time.sleep(1)
            except Exception as e:
                logging.exception("Scheduler encountered an error")
                send_telegram_message("⚠️ Scheduler error")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()


def schedule_daily_report(eod_time: str = "23:59"):
    schedule.every().day.at(eod_time).do(send_daily_report)


def send_daily_report():
    try:
        text = build_day_report_text()
        send_telegram_message(f"📒 Daily Report\n{text}")
    except Exception as e:
        logging.exception("Failed to build or send daily report")
        send_telegram_message("⚠️ Failed to generate daily report.")
