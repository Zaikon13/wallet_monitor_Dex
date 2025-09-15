#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py (stabilized)
- Minimal schedulers (intraday, EOD)
- Watchlist scanner (Dexscreener)
- Plain text Telegram to avoid MarkdownV2 traps
Keep it compact and green first.
"""
from __future__ import annotations
import os, time, json, signal, logging, threading
from datetime import timedelta
from core.watch import scan_watchlist, load_watchlist, save_watchlist
from telegram.api import send_telegram
from reports.day_report import build_day_report_text

# ---------- ENV ----------
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")
WATCH_POLL = int(os.getenv("WATCH_POLL", "120"))

INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR       = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE     = int(os.getenv("EOD_MINUTE", "59"))

TZ = os.getenv("TZ", "Europe/Athens")

# ---------- TZ helpers ----------
try:
    from zoneinfo import ZoneInfo
    TZINFO = ZoneInfo(TZ)
except Exception:
    TZINFO = None
from datetime import datetime
def now_dt():
    if TZINFO:
        return datetime.now(TZINFO)
    return datetime.now()
def ymd(dt=None):
    if dt is None: dt = now_dt()
    return dt.strftime("%Y-%m-%d")

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("sentinel")

# ---------- State ----------
shutdown_event = threading.Event()
WATCHLIST = load_watchlist(WATCHLIST_PATH)

# ---------- Loops ----------
def watchlist_loop():
    send_telegram("🔎 Watchlist scanner started.")
    while not shutdown_event.is_set():
        try:
            alerts = scan_watchlist(WATCHLIST)
            if alerts:
                from telegram.api import send_watchlist_alerts
                send_watchlist_alerts(alerts)
        except Exception as e:
            log.warning("watchlist error: %s", e)
        for _ in range(WATCH_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def intraday_report_loop():
    send_telegram("⏱ Intraday report…")
    last_sent = 0.0
    while not shutdown_event.is_set():
        if time.time() - last_sent >= INTRADAY_HOURS * 3600:
            try:
                text = build_day_report_text(
                    date_str=ymd(),
                    entries=[],
                    net_flow=0.0,
                    realized_today_total=0.0,
                    holdings_total=0.0,
                    breakdown=[],
                    unrealized=0.0,
                    data_dir=DATA_DIR,
                )
                send_telegram(text)
            except Exception as e:
                log.warning("intraday error: %s", e)
            last_sent = time.time()
        for _ in range(30):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def eod_report_loop():
    send_telegram(f"🕛 EOD scheduler active ({EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ}).")
    last_day = ""
    while not shutdown_event.is_set():
        now = now_dt()
        if now.strftime("%H:%M") == f"{EOD_HOUR:02d}:{EOD_MINUTE:02d}" and last_day != ymd(now):
            try:
                text = build_day_report_text(
                    date_str=ymd(now),
                    entries=[],
                    net_flow=0.0,
                    realized_today_total=0.0,
                    holdings_total=0.0,
                    breakdown=[],
                    unrealized=0.0,
                    data_dir=DATA_DIR,
                )
                send_telegram("🟢 End of Day Report\n" + text)
                last_day = ymd(now)
            except Exception as e:
                log.warning("EOD error: %s", e)
            time.sleep(62)  # avoid double-fire within same minute
        time.sleep(1)

# ---------- Entrypoint ----------
def main():
    send_telegram("✅ Sentinel boot (stabilized).")
    threads = [
        threading.Thread(target=watchlist_loop, name="watchlist", daemon=True),
        threading.Thread(target=intraday_report_loop, name="intraday", daemon=True),
        threading.Thread(target=eod_report_loop, name="eod", daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        while not shutdown_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown_event.set()
    for t in threads:
        t.join(timeout=5)

def _graceful(*_):
    shutdown_event.set()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)
    main()
