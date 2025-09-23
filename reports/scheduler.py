# reports/scheduler.py
import os, logging

try:
    import schedule  # type: ignore
    _HAVE_SCHEDULE = True
except Exception:
    _HAVE_SCHEDULE = False

from core.tz import now_local
from telegram.api import send_telegram_message

def _eod_tuple():
    hh = int(os.getenv("EOD_HOUR", "23"))
    mm = int(os.getenv("EOD_MINUTE", "59"))
    return hh, mm

def _send_daily_report():
    try:
        from reports.day_report import build_day_report_text
        text = build_day_report_text()
    except Exception:
        logging.exception("Failed to build daily report text")
        text = "(no report)"
    try:
        send_telegram_message(f"📒 Daily Report\n{text}")
    except Exception:
        logging.exception("Failed to send daily report")

# Lightweight fallback if schedule is missing
_last_run_date = None
_target_hhmm = None

def start_eod_scheduler():
    global _target_hhmm
    hh, mm = _eod_tuple()
    _target_hhmm = f"{hh:02d}:{mm:02d}"
    if _HAVE_SCHEDULE:
        schedule.every().day.at(_target_hhmm).do(_send_daily_report)
    else:
        logging.warning("python 'schedule' not installed: using fallback scheduler")
    return _target_hhmm

def run_pending():
    if _HAVE_SCHEDULE:
        schedule.run_pending()
        return
    # Fallback: run once per day at hh:mm (app time)
    global _last_run_date, _target_hhmm
    now = now_local()
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    if _target_hhmm and hhmm == _target_hhmm and _last_run_date != today:
        _send_daily_report()
        _last_run_date = today
