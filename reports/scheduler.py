# reports/scheduler.py
import os, logging, schedule
from datetime import time as dtime
from core.tz import now_gr
from reports.day_report import build_day_report_text
from telegram.api import send_telegram_message

def _eod_tuple():
    # ENV overrides; default 23:59
    hh = int(os.getenv("EOD_HOUR", "23"))
    mm = int(os.getenv("EOD_MINUTE", "59"))
    return hh, mm

def _send_daily_report():
    try:
        text = build_day_report_text()
        send_telegram_message(f"📒 Daily Report\n{text}")
    except Exception as e:
        logging.exception("Failed to build or send daily report")
        send_telegram_message("⚠️ Failed to generate daily report.")

def start_eod_scheduler():
    hh, mm = _eod_tuple()
    at = f"{hh:02d}:{mm:02d}"
    # Run once per day at configured local time
    schedule.every().day.at(at).do(_send_daily_report)
    return at

def run_pending():
    # Call this regularly from your main loop
    schedule.run_pending()
