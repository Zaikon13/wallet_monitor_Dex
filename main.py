#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py (v5 merged — fixed)
Cronos DeFi Sentinel — Wallet + DEX watchlist + Schedulers
- Watchlist scanner (Dexscreener) με cooldowns & thresholds
- Daily/Intraday/EOD reports (χρήση reports.day_report.build_day_report)
- Lightweight, <1000 lines
"""

from __future__ import annotations
import os, sys, time, json, signal, logging, threading
from collections import defaultdict, deque
from decimal import Decimal, getcontext

# Local imports
from core.config import apply_env_aliases
from core.tz import tz_init, now_dt, ymd
from telegram.api import send_telegram
from reports.day_report import build_day_report
from core.watch import scan_watchlist, load_watchlist

# ---- Precision ----
getcontext().prec = 28

# ===================== ENV & Setup =====================
apply_env_aliases()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

DATA_DIR   = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

# Timings
INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR       = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE     = int(os.getenv("EOD_MINUTE", "59"))

# Watchlist poll
WATCH_POLL = int(os.getenv("WATCH_POLL", "120"))

# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sentinel")

# ---- TZ ----
LOCAL_TZ = tz_init()

# ===================== Runtime state =====================
shutdown_event = threading.Event()

# For completeness (kept minimal in this trimmed main)
_token_balances: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
_token_meta: dict[str, dict] = {}
_seen_tx_hashes: set[str] = set()

# watchlist
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")
WATCHLIST = load_watchlist(WATCHLIST_PATH)

EPSILON = Decimal("1e-12")

# ===================== Watchlist Scanner =====================
def watchlist_loop():
    log.info("Watchlist scanner started.")
    while not shutdown_event.is_set():
        try:
            alerts = scan_watchlist(WATCHLIST)
            if alerts:
                from telegram.api import send_watchlist_alerts
                send_watchlist_alerts(alerts)
        except Exception as e:
            log.warning("watchlist loop error: %s", e)
        for _ in range(max(1, WATCH_POLL)):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ===================== Scheduler =====================
def intraday_report_loop():
    log.info("Intraday scheduler active.")
    last_sent = 0.0
    while not shutdown_event.is_set():
        try:
            now_ts = time.time()
            if now_ts - last_sent >= INTRADAY_HOURS * 3600:
                send_telegram("⏱ Intraday report…")
                txt = build_day_report()
                send_telegram(txt)
                last_sent = now_ts
        except Exception as e:
            log.warning("Intraday loop error: %s", e)
        for _ in range(30):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def eod_report_loop():
    log.info("EOD scheduler active.")
    last_sent_date = ""
    while not shutdown_event.is_set():
        now = now_dt()
        if now.strftime("%H:%M") == f"{EOD_HOUR:02d}:{EOD_MINUTE:02d}" and last_sent_date != ymd(now):
            try:
                send_telegram("🌙 End-of-day report…")
                txt = build_day_report()
                send_telegram(txt)
                last_sent_date = ymd(now)
            except Exception as e:
                log.warning("EOD loop error: %s", e)
        for _ in range(10):
            if shutdown_event.is_set():
                break
            time.sleep(6)

# ===================== Entrypoint =====================
def main():
    # Startup message
    send_telegram("✅ Sentinel started (v5-fixed).")
    # Threads
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

def _graceful_exit(*_):
    shutdown_event.set()

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    main()
