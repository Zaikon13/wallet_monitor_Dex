import os, schedule
from reports.day_report import build_day_report_text
from telegram.api import send_telegram_message
def _daily(): 
    try: send_telegram_message("📒 Daily Report\n"+build_day_report_text(False))
    except Exception: send_telegram_message("⚠️ Failed to generate daily report.")
def _intraday():
    try: send_telegram_message("🕒 Intraday Report\n"+build_day_report_text(True))
    except Exception: pass
def start_eod_scheduler():
    eod=os.getenv("EOD_TIME","23:59"); schedule.every().day.at(eod).do(_daily)
    intr=int(os.getenv("INTRADAY_HOURS","3") or 3)
    if intr>0: schedule.every(intr).hours.do(_intraday)
    return eod
def run_pending(): schedule.run_pending()
