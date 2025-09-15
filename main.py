#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py (stabilized)
- Safe imports (local-first), fallbacks & shims
- Watchlist scanner (if core/watch.py υπάρχει)
- Intraday & EOD schedulers (χρησιμοποιεί build_day_report ή fallback)
- Threading-safe: αν δεν γίνεται spawn threads, τρέχει single-pass και τερματίζει OK
"""

from __future__ import annotations
import os, sys, time, json, signal, logging, threading
from datetime import datetime, timedelta
from decimal import Decimal, getcontext

# ---------- Ensure local packages win over site-packages ----------
try:
    ROOT = os.path.dirname(os.path.abspath(__file__))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
except Exception:
    pass

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("sentinel")

# ---------- Precision ----------
getcontext().prec = 28
EPSILON = Decimal("1e-12")

# ---------- Safe timezone ----------
def _init_tz():
    tz_env = os.getenv("TZ", "Europe/Athens")
    try:
        from zoneinfo import ZoneInfo
        LOCAL_TZ = ZoneInfo(tz_env)
        def _now():  # tz-aware
            return datetime.now(LOCAL_TZ)
        return LOCAL_TZ, _now
    except Exception:
        log.warning("Timezone '%s' not found; falling back to naive local time.", tz_env)
        def _now():
            return datetime.now()
        return None, _now

LOCAL_TZ, now_dt = _init_tz()

def ymd(dt: datetime | None = None) -> str:
    return (dt or now_dt()).strftime("%Y-%m-%d")

# ---------- Env / paths ----------
DATA_DIR   = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR       = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE     = int(os.getenv("EOD_MINUTE", "59"))

WATCH_POLL     = int(os.getenv("WATCH_POLL", "120"))
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")

# ---------- Safe imports with fallbacks ----------
# core.config (optional aliases)
try:
    from core.config import apply_env_aliases
    apply_env_aliases()
except Exception:
    pass

# reports.day_report (build_day_report preferred)
def _fallback_report() -> str:
    return f"*📒 Daily Report* ({ymd()})\n_No reporting module available yet._"

try:
    from reports.day_report import build_day_report as _build_report
    def build_report_text():
        try:
            return _build_report()
        except TypeError:
            # some versions expect args; best-effort call without params
            return _build_report()
except Exception:
    try:
        from reports.day_report import build_day_report_text as _build_report_txt
        def build_report_text():
            try:
                # older signature often requires kwargs; call empty (module should read DATA_DIR internally)
                return _build_report_txt()
            except Exception:
                return _fallback_report()
    except Exception:
        build_report_text = _fallback_report

# telegram.api (send_telegram + optional send_watchlist_alerts)
def _noop_send(msg: str):
    log.info("[telegram noop] %s", msg.replace("\n", "  "))

try:
    from telegram.api import send_telegram as _send_telegram
except Exception:
    _send_telegram = _noop_send

try:
    from telegram.api import send_watchlist_alerts as _send_watchlist_alerts
except Exception:
    def _send_watchlist_alerts(alerts: list[dict]):
        if alerts:
            _send_telegram("🔍 Watchlist Alerts\n" + json.dumps(alerts, ensure_ascii=False, indent=2))

send_telegram = _send_telegram
send_watchlist_alerts = _send_watchlist_alerts

# core.watch (watchlist load/scan)
try:
    from core.watch import load_watchlist, scan_watchlist
except Exception:
    def load_watchlist(_path: str) -> list[str]:
        return []
    def scan_watchlist(_items: list[str]) -> list[dict]:
        return []

# ---------- Runtime state ----------
shutdown_event = threading.Event()

# ---------- Watchlist loop ----------
def watchlist_loop():
    wl = []
    try:
        wl = load_watchlist(WATCHLIST_PATH)
    except Exception as e:
        log.warning("load_watchlist failed: %s", e)
    if not wl:
        log.info("Watchlist is empty; scanner idle (every %ss).", WATCH_POLL)

    while not shutdown_event.is_set():
        try:
            alerts = scan_watchlist(wl) if wl else []
            if alerts:
                send_watchlist_alerts(alerts)
        except Exception as e:
            log.warning("watchlist loop error: %s", e)
        for _ in range(max(1, WATCH_POLL)):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ---------- Intraday / EOD ----------
def intraday_report_loop():
    send_telegram("⏱ Intraday reporting enabled.")
    last = 0.0
    while not shutdown_event.is_set():
        try:
            now_ts = time.time()
            if now_ts - last >= max(1, INTRADAY_HOURS) * 3600:
                send_telegram("⏱ Intraday report…")
                send_telegram(build_report_text())
                last = now_ts
        except Exception as e:
            log.warning("Intraday error: %s", e)
        for _ in range(30):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def eod_report_loop():
    send_telegram(f"🕛 EOD scheduler active at {EOD_HOUR:02d}:{EOD_MINUTE:02d}.")
    last_sent_date = ""
    while not shutdown_event.is_set():
        try:
            now = now_dt()
            if now.strftime("%H:%M") == f"{EOD_HOUR:02d}:{EOD_MINUTE:02d}" and last_sent_date != ymd(now):
                send_telegram("🌙 End-of-day report…")
                send_telegram(build_report_text())
                last_sent_date = ymd(now)
        except Exception as e:
            log.warning("EOD error: %s", e)
        for _ in range(10):
            if shutdown_event.is_set():
                break
            time.sleep(6)

# ---------- Entrypoint ----------
def main():
    # sanity banner
    send_telegram("✅ Sentinel booting (stabilized).")
    threads = []
    try:
        threads.append(threading.Thread(target=watchlist_loop, name="watchlist", daemon=True))
        threads.append(threading.Thread(target=intraday_report_loop, name="intraday", daemon=True))
        threads.append(threading.Thread(target=eod_report_loop, name="eod", daemon=True))
        for t in threads:
            t.start()
    except Exception as e:
        # περιβάλλον που δεν επιτρέπει threads: τρέχουμε single-pass και τερματίζουμε ΟΚ
        log.warning("Threading unavailable (%s). Running single-pass.", e)
        try:
            # single pass scan + one report
            alerts = scan_watchlist(load_watchlist(WATCHLIST_PATH))
            if alerts:
                send_watchlist_alerts(alerts)
            send_telegram(build_report_text())
            send_telegram("✅ Single-pass completed.")
            return
        except Exception as ex:
            log.error("Single-pass failed: %s", ex)
            return

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
