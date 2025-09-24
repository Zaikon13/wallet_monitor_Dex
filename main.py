# main.py
# Drop-in runtime for Zaikon13/wallet_monitor_Dex
# Uses only the exported symbols that exist in the repo:
# - core.holdings.get_wallet_snapshot
# - core.runtime_state.update_snapshot, note_tick
# - core.providers.cronos.fetch_wallet_txs (optional)
# - reports.day_report.build_day_report_text
# - reports.weekly.build_weekly_report_text
# - reports.scheduler.run_pending (optional)
# - telegram.api.send_telegram, send_telegram_messages, telegram_long_poll_loop
# - python-schedule if available
#
# Behavior:
# - Plain-text Telegram sends (no MarkdownV2).
# - TG long-poll runs in a daemon thread (if available).
# - Intraday tick: snapshot -> day report -> send (first tick immediate).
# - EOD daily report & weekly report scheduled via schedule (if available).
# - Health ping scheduled.
# - Defensive: tolerates missing optional modules and varying function signatures.

import os
import time
import threading
import logging
from decimal import Decimal, getcontext

getcontext().prec = 28
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("main")

# ---------- Imports from repo (guarded) ----------
try:
    from core.holdings import get_wallet_snapshot
except Exception as e:
    get_wallet_snapshot = None
    log.error("Missing core.holdings.get_wallet_snapshot: %s", e)

try:
    from core.runtime_state import update_snapshot, note_tick
except Exception as e:
    update_snapshot = lambda *_: None
    note_tick = lambda *_: None
    log.warning("Missing core.runtime_state helpers: %s", e)

try:
    from core.providers.cronos import fetch_wallet_txs
except Exception:
    fetch_wallet_txs = None

try:
    from reports.day_report import build_day_report_text
except Exception as e:
    build_day_report_text = None
    log.error("Missing reports.day_report.build_day_report_text: %s", e)

try:
    from reports.weekly import build_weekly_report_text
except Exception:
    build_weekly_report_text = None

try:
    from reports.scheduler import run_pending as reports_run_pending
except Exception:
    reports_run_pending = None

try:
    from telegram.api import send_telegram, send_telegram_messages, telegram_long_poll_loop
except Exception as e:
    send_telegram = None
    send_telegram_messages = None
    telegram_long_poll_loop = None
    log.error("Missing telegram.api exports: %s", e)

try:
    import schedule
except Exception:
    schedule = None
    log.info("python-schedule not available; scheduled jobs disabled")

# ---------- ENV helpers ----------
def _env_str(key: str, default: str = "") -> str:
    v = os.getenv(key)
    return v if v not in (None, "") else default

def _env_int(key: str, default: int = 0) -> int:
    try:
        return int(os.getenv(key, str(default)) or default)
    except Exception:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)) or default)
    except Exception:
        return default

def _start_thread(target, name: str):
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t

# ---------- Safe Telegram send helpers ----------
def _safe_send(text: str):
    """Send a single plain-text message. Returns (ok, detail)."""
    if not send_telegram:
        log.warning("send_telegram not available; message suppressed: %.100s", text)
        return False, "no-telegram"
    try:
        # repo's send_telegram may already return (ok, detail) or just truthy
        res = send_telegram(text)
        return res if isinstance(res, tuple) else (True, "sent")
    except Exception as e:
        log.exception("send_telegram failed")
        return False, str(e)

def _safe_send_chunked(text: str, max_chunk: int = 3500):
    """Chunk by paragraphs to avoid message size limits; prefer send_telegram_messages if available."""
    if not text:
        return _safe_send("")
    if send_telegram_messages:
        try:
            # assume function accepts list[str]
            parts = []
            acc = []
            size = 0
            for ln in text.splitlines():
                size += len(ln) + 1
                acc.append(ln)
                if size > max_chunk:
                    parts.append("\n".join(acc))
                    acc = []
                    size = 0
            if acc:
                parts.append("\n".join(acc))
            if parts:
                return send_telegram_messages(parts)
        except Exception:
            log.exception("send_telegram_messages failed; falling back to single sends")
    # fallback: naive slicing
    if len(text) <= max_chunk:
        return _safe_send(text)
    parts = []
    i = 0
    L = len(text)
    while i < L:
        parts.append(text[i:i+max_chunk])
        i += max_chunk
    for p in parts:
        _safe_send(p)
    return True, "chunked"

# ---------- Snapshot / state helpers ----------
def _get_snapshot():
    """Return wallet snapshot dict or {}. Update runtime state if possible."""
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
        # non-fatal
        pass
    return snap

# ---------- Report builders ----------
def _build_day_report(snap):
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

def _build_weekly_report():
    if not build_weekly_report_text:
        return "Weekly report not available."
    try:
        return build_weekly_report_text()
    except Exception:
        log.exception("build_weekly_report_text failed")
        return "Failed to build weekly report."

# ---------- Workers ----------
def _schedule_worker():
    """Drive schedule and optional reports.scheduler.run_pending."""
    if not schedule and not reports_run_pending:
        log.info("No schedule or reports_run_pending; schedule_worker exiting.")
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
            log.exception("schedule worker loop error")
        time.sleep(1)

def _tg_poll_worker():
    if not telegram_long_poll_loop:
        log.info("telegram_long_poll_loop not available; skipping long-poll")
        return
    try:
        telegram_long_poll_loop()
    except Exception:
        log.exception("telegram_long_poll_loop crashed")

# core tick job
def _intraday_tick():
    try:
        snap = _get_snapshot()
        report = _build_day_report(snap)
        if not report:
            report = "No transactions yet today."
        # prefix with short header
        text = "🕒 Intraday Update\n" + report
        # send chunked if big
        _safe_send_chunked(text)
        try:
            note_tick()
        except Exception:
            pass
    except Exception:
        log.exception("intraday tick failed")
        try:
            _safe_send("⚠️ runtime error (intraday)")
        except Exception:
            pass

# EOD job
def _eod_job():
    try:
        snap = _get_snapshot()
        text = _build_day_report(snap)
        _safe_send_chunked("📒 Daily Report\n" + (text or "(empty)"))
    except Exception:
        log.exception("EOD job failed")
        try:
            _safe_send("⚠️ Failed to generate daily report.")
        except Exception:
            pass
    return True

# Weekly job wrapper checks day-of-week
def _weekly_job(target_dow: str):
    def _job():
        try:
            import datetime as _dt
            dow_now = _dt.datetime.now().strftime("%a").upper()  # e.g., 'SUN'
            if dow_now == target_dow.upper():
                text = _build_weekly_report()
                _safe_send_chunked("🗓 Weekly Report\n" + text)
        except Exception:
            log.exception("weekly_job failed")
        return True
    return _job

# health ping
def _health_ping():
    try:
        _safe_send("✅ alive")
    except Exception:
        pass
    return True

# ---------- Main runner ----------
def run():
    log.info("🟢 Starting Wallet Monitor (Cronos DeFi Sentinel)")

    # Start TG long-poll (daemon)
    _start_thread(_tg_poll_worker, "tg-long-poll")

    # Startup ping
    try:
        _safe_send("✅ Cronos DeFi Sentinel started and is online.")
    except Exception:
        log.exception("startup ping failed")

    # Schedule jobs if schedule present
    if schedule:
        EOD_TIME = _env_str("EOD_TIME", "23:59")
        HEALTH_MIN = _env_int("HEALTH_MIN", 30)
        WEEKLY_DOW = _env_str("WEEKLY_DOW", "SUN")
        WEEKLY_TIME = _env_str("WEEKLY_TIME", "18:00")

        try:
            schedule.every().day.at(EOD_TIME).do(_eod_job)
            log.info("daily report scheduled at %s", EOD_TIME)
        except Exception:
            log.exception("failed to bind EOD schedule")

        try:
            schedule.every(HEALTH_MIN).minutes.do(_health_ping)
            log.info("health ping scheduled every %s minute(s)", HEALTH_MIN)
        except Exception:
            log.exception("failed to bind health schedule")

        # weekly
        try:
            schedule.every().day.at(WEEKLY_TIME).do(_weekly_job(WEEKLY_DOW))
            log.info("weekly report scheduled for %s at %s", WEEKLY_DOW, WEEKLY_TIME)
        except Exception:
            log.exception("failed to bind weekly schedule")

        # Start the schedule worker thread
        _start_thread(_schedule_worker, "schedule-worker")
    else:
        log.info("python-schedule not installed; scheduled jobs disabled")

    # Immediate first tick
    try:
        _intraday_tick()
    except Exception:
        log.exception("first intraday tick failed")

    # Main sleep loop (intraday cadence)
    intraday_hours = _env_float("INTRADAY_HOURS", 1.0)
    sleep_s = max(60, int(intraday_hours * 3600))
    while True:
        try:
            _intraday_tick()
        except Exception:
            log.exception("runtime intraday error")
            try:
                _safe_send("⚠️ runtime error (throttled)")
            except Exception:
                pass
        log.info("sleeping %ss until next tick", sleep_s)
        time.sleep(sleep_s)

if __name__ == "__main__":
    run()
