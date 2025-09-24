# main.py — drop-in for Zaikon13/wallet_monitor_Dex
# Wires ONLY to exports confirmed in your repo:
# core.wallet_monitor.make_wallet_monitor
# core.holdings.get_wallet_snapshot
# core.runtime_state.update_snapshot, note_tick
# reports.day_report.build_day_report_text
# reports.weekly.build_weekly_report_text
# reports.scheduler.run_pending  (optional)
# telegram.api.send_telegram, send_telegram_messages, telegram_long_poll_loop
# schedule (python-schedule) if present
#
# Behavior:
# - Starts TG long-poll thread (if available)
# - Starts schedule worker thread (python-schedule + optional reports.scheduler.run_pending)
# - Immediate first tick: wallet monitor poll + day report
# - Intraday loop with clear logs
# - EOD & Weekly jobs
# - Health ping
# - All Telegram sends are PLAIN TEXT

import os
import time
import logging
import threading
from decimal import Decimal, getcontext

getcontext().prec = 28
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("main")

# ==== Imports guarded to your actual modules ====
try:
    from core.wallet_monitor import make_wallet_monitor  # exists
except Exception as e:
    make_wallet_monitor = None
    log.error("core.wallet_monitor.make_wallet_monitor unavailable: %s", e)

try:
    from core.holdings import get_wallet_snapshot  # exists
except Exception as e:
    get_wallet_snapshot = None
    log.error("core.holdings.get_wallet_snapshot unavailable: %s", e)

try:
    from core.runtime_state import update_snapshot, note_tick  # exist
except Exception as e:
    def update_snapshot(_): pass
    def note_tick(): pass
    log.warning("core.runtime_state helpers unavailable: %s", e)

try:
    from reports.day_report import build_day_report_text  # exists
except Exception as e:
    build_day_report_text = None
    log.error("reports.day_report.build_day_report_text unavailable: %s", e)

try:
    from reports.weekly import build_weekly_report_text  # exists
except Exception:
    build_weekly_report_text = None

try:
    from reports.scheduler import run_pending as reports_run_pending  # exists
except Exception:
    reports_run_pending = None

try:
    from telegram.api import (
        send_telegram,
        send_telegram_messages,
        telegram_long_poll_loop,
    )  # exist
except Exception as e:
    send_telegram = None
    send_telegram_messages = None
    telegram_long_poll_loop = None
    log.error("telegram.api not available: %s", e)

try:
    import schedule  # python-schedule
except Exception:
    schedule = None
    log.info("python-schedule not installed; timers disabled")

# ==== ENV helpers ====
def _env_str(k: str, d: str = "") -> str:
    v = os.getenv(k)
    return v if v not in (None, "") else d

def _env_int(k: str, d: int) -> int:
    try:
        return int(os.getenv(k, str(d)) or d)
    except Exception:
        return d

def _env_float(k: str, d: float) -> float:
    try:
        return float(os.getenv(k, str(d)) or d)
    except Exception:
        return d

def _start_thread(target, name: str):
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t

# ==== Telegram helpers (plain text) ====
def _send(msg: str):
    if not send_telegram:
        log.warning("Telegram unavailable; msg suppressed: %.120s", msg)
        return False, "no-telegram"
    try:
        r = send_telegram(msg)
        return r if isinstance(r, tuple) else (True, "sent")
    except Exception as e:
        log.exception("send_telegram failed")
        return False, str(e)

def _send_many(parts):
    if not parts:
        return True, "empty"
    if send_telegram_messages:
        try:
            return send_telegram_messages(parts)
        except Exception:
            log.exception("send_telegram_messages failed; falling back")
    ok = True
    for p in parts:
        r, _ = _send(p)
        ok = ok and r
    return ok, "fallback"

def _chunk_and_send(title: str, body: str, max_len: int = 3500):
    text = (title + "\n" + body) if body else title
    if len(text) <= max_len:
        return _send(text)
    # paragraph chunking
    parts, acc, size = [], [], 0
    for ln in text.splitlines():
        size += len(ln) + 1
        acc.append(ln)
        if size > max_len - 200:
            parts.append("\n".join(acc)); acc=[]; size=0
    if acc: parts.append("\n".join(acc))
    return _send_many(parts)

# ==== Snapshot / monitor ====
def _snapshot():
    """Return wallet snapshot dict and update runtime_state."""
    if not get_wallet_snapshot:
        return {}
    addr = _env_str("WALLET_ADDRESS", "")
    rpc  = _env_str("RPC", "")
    try:
        try:
            snap = get_wallet_snapshot(addr, rpc)
        except TypeError:
            snap = get_wallet_snapshot(addr)
    except Exception:
        log.exception("get_wallet_snapshot failed")
        snap = {}
    try:
        update_snapshot(snap)
    except Exception:
        pass
    return snap

def _make_monitor():
    if not make_wallet_monitor:
        return None
    try:
        # make_from_env exists per your list but monitor factory is what we call here
        mon = make_wallet_monitor()
        return mon
    except Exception:
        log.exception("make_wallet_monitor failed")
        return None

# ==== Reports ====
def _day_text(snap):
    if not build_day_report_text:
        return "Daily report not available."
    try:
        try:
            return build_day_report_text(snap)
        except TypeError:
            return build_day_report_text()
    except Exception:
        log.exception("build_day_report_text failed")
        return "Failed to build daily report."

def _weekly_text():
    if not build_weekly_report_text:
        return "Weekly report not available."
    try:
        return build_weekly_report_text()
    except Exception:
        log.exception("build_weekly_report_text failed")
        return "Failed to build weekly report."

# ==== Workers ====
def _schedule_worker():
    if not schedule and not reports_run_pending:
        log.info("No schedule nor reports_run_pending; schedule worker exits.")
        return
    while True:
        try:
            if schedule:
                schedule.run_pending()
            if reports_run_pending:
                try:
                    reports_run_pending()
                except Exception:
                    log.exception("reports.scheduler.run_pending error")
        except Exception:
            log.exception("schedule worker error")
        time.sleep(1)

def _tg_worker():
    if not callable(telegram_long_poll_loop):
        log.info("telegram_long_poll_loop missing; skip TG thread")
        return
    try:
        telegram_long_poll_loop()
    except Exception:
        log.exception("telegram long-poll crashed")

# One intraday cycle: poll monitor, build/send day report
def _tick(mon):
    try:
        if mon is not None:
            try:
                mon.poll_once()
            except Exception:
                log.exception("wallet_monitor.poll_once failed")
        snap = _snapshot()
        day = _day_text(snap)
        if not day:
            day = "No transactions yet today."
        _chunk_and_send("🕒 Intraday Update", day)
        try:
            note_tick()
        except Exception:
            pass
    except Exception:
        log.exception("intraday tick failed")
        _send("⚠️ runtime error (intraday)")

def _eod_job():
    try:
        snap = _snapshot()
        text = _day_text(snap)
        _chunk_and_send("📒 Daily Report", text or "(empty)")
    except Exception:
        log.exception("EOD failed")
        _send("⚠️ Failed to generate daily report.")
    return True

def _weekly_job(target_dow: str):
    def _run():
        try:
            import datetime as _dt
            if _dt.datetime.now().strftime("%a").upper() == target_dow.upper():
                _chunk_and_send("🗓 Weekly Report", _weekly_text())
        except Exception:
            log.exception("weekly job failed")
        return True
    return _run

def _health_job():
    _send("✅ alive")
    return True

# ==== Main ====
def run():
    log.info("🟢 Starting Wallet Monitor")

    mon = _make_monitor()

    # TG long-poll
    _start_thread(_tg_worker, "tg-long-poll")

    # Startup ping
    _send("✅ Cronos DeFi Sentinel started and is online.")

    # Scheduling
    if schedule:
        EOD_TIME    = _env_str("EOD_TIME", "23:59")
        HEALTH_MIN  = _env_int("HEALTH_MIN", 30)
        WEEKLY_DOW  = _env_str("WEEKLY_DOW", "SUN")
        WEEKLY_TIME = _env_str("WEEKLY_TIME", "18:00")

        try:
            schedule.every().day.at(EOD_TIME).do(_eod_job)
            log.info("daily report scheduled at %s", EOD_TIME)
        except Exception:
            log.exception("bind EOD failed")

        try:
            schedule.every(HEALTH_MIN).minutes.do(_health_job)
            log.info("health ping scheduled every %s minute(s)", HEALTH_MIN)
        except Exception:
            log.exception("bind health failed")

        try:
            schedule.every().day.at(WEEKLY_TIME).do(_weekly_job(WEEKLY_DOW))
            log.info("weekly report scheduled for %s at %s", WEEKLY_DOW, WEEKLY_TIME)
        except Exception:
            log.exception("bind weekly failed")

        _start_thread(_schedule_worker, "schedule-worker")
    else:
        log.info("scheduling disabled (no schedule module)")

    # First tick then loop
    try:
        _tick(mon)
    except Exception:
        log.exception("first tick failed")

    sleep_s = max(60, int(_env_float("INTRADAY_HOURS", 1.0) * 3600))
    while True:
        _tick(mon)
        log.info("sleeping %ss until next tick", sleep_s)
        time.sleep(sleep_s)

if __name__ == "__main__":
    run()
