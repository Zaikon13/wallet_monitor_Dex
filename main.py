#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, time, logging, signal
from datetime import datetime
from typing import Optional
import requests
from dotenv import load_dotenv  # pip: python-dotenv
import schedule                 # pip: schedule

# ---------- Logging ----------
def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

# ---------- Env ----------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def get_eod_time() -> str:
    t = os.getenv("EOD_TIME", "23:59").strip()
    try:
        hh, mm = t.split(":")
        ih, im = int(hh), int(mm)
        if 0 <= ih <= 23 and 0 <= im <= 59:
            return f"{ih:02d}:{im:02d}"
    except Exception:
        pass
    logging.warning("Invalid EOD_TIME '%s' → using 23:59", t)
    return "23:59"

# ---------- Telegram (plain text, no Markdown) ----------
def send_telegram(text: str) -> None:
    bot = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        logging.info("Telegram not configured (TELEGRAM_BOT_TOKEN/CHAT_ID missing)")
        return
    url = f"https://api.telegram.org/bot{bot}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code != 200:
            logging.warning("Telegram send failed %s: %s", r.status_code, r.text)
    except Exception as e:
        logging.exception("Telegram send error: %s", e)

# ---------- Jobs ----------
def job_startup() -> None:
    send_telegram("✅ Sentinel started (rescue boot).")

def job_daily_report() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    msg = f"📒 Daily Report — {today}\nStatus: OK"
    send_telegram(msg)

# ---------- Loop ----------
def bind_schedules() -> None:
    schedule.every().day.at(get_eod_time()).do(job_daily_report)
    logging.info("Scheduled daily report at %s", get_eod_time())

def run_loop(stop: list[bool]) -> None:
    while not stop[0]:
        try:
            schedule.run_pending()
        except Exception as e:
            logging.exception("schedule.run_pending failed: %s", e)
        time.sleep(1)

def install_signals(stop: list[bool]) -> None:
    def _grace(sig, frame):
        logging.info("Signal %s — stopping...", sig)
        stop[0] = True
        try: send_telegram("🛑 Sentinel stopping...")
        except Exception: pass
    try:
        signal.signal(signal.SIGINT, _grace)
        signal.signal(signal.SIGTERM, _grace)
    except Exception:
        pass

# ---------- Main ----------
def main() -> int:
    setup_logging()
    try: load_dotenv(override=False)
    except Exception: pass

    logging.info("Starting Sentinel (rescue boot)...")
    job_startup()
    bind_schedules()

    stop = [False]
    install_signals(stop)
    try:
        run_loop(stop)
    except Exception as e:
        logging.exception("Fatal in main loop: %s", e)
        try: send_telegram("❌ Fatal error in main loop (rescue boot).")
        except Exception: pass
        return 1
    logging.info("Exit.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
