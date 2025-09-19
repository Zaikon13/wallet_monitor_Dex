#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (thin orchestrator)
- Boot, threads, schedulers, graceful exit
- Business logic lives in: core/*, reports/*, telegram/*
"""

import os, sys, time, json, threading, logging, signal
from datetime import datetime
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# ── Load env ───────────────────────────────────────────────────────────────────
load_dotenv()
TZ = os.getenv("TZ", "Europe/Athens")
def _init_tz(tz_str: str):
    os.environ["TZ"] = tz_str
    try:
        import time as _t
        if hasattr(_t, "tzset"):
            _t.tzset()
    except Exception:
        pass
    return ZoneInfo(tz_str)
LOCAL_TZ = _init_tz(TZ)
def now_dt(): return datetime.now(LOCAL_TZ)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("wallet-monitor")

# ── Imports from modules ───────────────────────────────────────────────────────
from telegram.api import send_telegram
from telegram.commands import telegram_long_poll_loop, handle_external_watch_cmd
from core.discovery import discovery_loop, monitor_tracked_pairs_loop
from core.alerts import alerts_monitor_loop, guard_monitor_loop
from core.holdings import replay_today_cost_basis
from core.holdings import format_daily_sum_message, build_day_report_text
from core.holdings import WALLET_POLL, wallet_monitor_loop
from core.discovery import DEX_POLL
from core.alerts import ALERTS_INTERVAL_MIN

# ── Shutdown flag & scheduler ──────────────────────────────────────────────────
shutdown_event = threading.Event()
INTRADAY_HOURS  = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR        = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE      = int(os.getenv("EOD_MINUTE", "59"))

def _scheduler_loop():
    send_telegram("⏱ Scheduler online (intraday/EOD).")
    last_intraday_sent = 0.0
    while not shutdown_event.is_set():
        try:
            now = now_dt()
            # Intraday summary
            if last_intraday_sent <= 0 or (time.time() - last_intraday_sent) >= INTRADAY_HOURS*3600:
                send_telegram(format_daily_sum_message()); last_intraday_sent = time.time()
            # EOD report
            if now.hour == EOD_HOUR and now.minute == EOD_MINUTE:
                send_telegram(build_day_report_text())
                time.sleep(65)  # avoid duplicates in the same minute
        except Exception as e:
            log.debug("scheduler error: %s", e)
        # light sleep loop (responsive to shutdown)
        for _ in range(20):
            if shutdown_event.is_set(): break
            time.sleep(3)

def _graceful_exit(signum, frame):
    try: send_telegram("🛑 Shutting down.")
    except: pass
    shutdown_event.set()

def main():
    send_telegram("🟢 Starting Cronos DeFi Sentinel.")
    # prime daily cost-basis (safe if file missing)
    try: replay_today_cost_basis()
    except Exception as e: log.debug("replay_today_cost_basis: %s", e)

    # threads
    threading.Thread(target=discovery_loop,            name="discovery", daemon=True).start()
    threading.Thread(target=monitor_tracked_pairs_loop,name="dex",       daemon=True).start()
    threading.Thread(target=alerts_monitor_loop,       name="alerts",    daemon=True).start()
    threading.Thread(target=guard_monitor_loop,        name="guard",     daemon=True).start()
    threading.Thread(target=telegram_long_poll_loop,   name="telegram",  daemon=True).start()
    threading.Thread(target=wallet_monitor_loop,       name="wallet",    daemon=True).start()
    threading.Thread(target=_scheduler_loop,           name="scheduler", daemon=True).start()

    # keep alive
    while not shutdown_event.is_set():
        time.sleep(1)

if __name__ == "__main__":
    signal.signal(signal.SIGINT,  _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log.exception("fatal: %s", e)
        try: send_telegram(f"💥 Fatal error: {e}")
        except: pass
        sys.exit(1)
