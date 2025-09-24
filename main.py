# main.py
# Wallet Monitor — runtime-stable launcher wired to the repo’s actual APIs.
# - Plain-text Telegram sends (no MarkdownV2).
# - TG long-poll thread.
# - Intraday tick -> snapshot -> day report to Telegram.
# - EOD day report & weekly report schedulers.
# - Health ping.
# - Defensive calls: tolerate differing call signatures and missing optional pieces.

import os
import time
import threading
import logging
from decimal import Decimal, getcontext

getcontext().prec = 28
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("main")

# ── Repo APIs that EXIST (per your exports) ─────────────────────────────────────
try:
    from core.holdings import get_wallet_snapshot                    # OK
except Exception as e:
    get_wallet_snapshot = None
    log.error("core.holdings.get_wallet_snapshot unavailable: %s", e)

try:
    from core.runtime_state import update_snapshot, note_tick         # OK
except Exception as e:
    def update_snapshot(_): pass
    def note_tick(): pass
    log.warning("core.runtime_state helpers unavailable: %s", e)

try:
    from core.providers.cronos import fetch_wallet_txs                # OK (optional use)
except Exception:
    fetch_wallet_txs = None

try:
    from core.watch import make_from_env                              # OK (optional use)
except Exception:
    make_from_env = None

try:
    from reports.day_report import build_day_report_text              # OK
except Exception as e:
    build_day_report_text = None
    log.error("reports.day_report.build_day_report_text unavailable: %s", e)

try:
    from reports.weekly import build_weekly_report_text               # OK
except Exception:
    build_weekly_report_text = None

try:
    from reports.scheduler import run_pending as scheduler_run_pending  # OK (optional)
except Exception:
    scheduler_run_pending = None

try:
    from telegram.api import send_telegram, send_telegram_messages, telegram_long_poll_loop  # OK
except Exception as e:
    send_telegram = None
    send_telegram_messages = None
    telegram_long_poll_loop = None
    log.error("telegram.api unavailable: %s", e)

try:
    import schedule  # python-schedule
except Exception as e:
    schedule = None
    log.error("schedule module not available: %s", e)

# ── ENV helpers ────────────────────────────────────────────────────────────────
def _env_str(k: str, default: str = "") -> str:
    v = os.getenv(k)
    return v if v not in (None, "") else default

def _env_float(k: str, default: float) -> float:
    try:
        return float(os.getenv(k, str(default)) or default)
    except Exception:
        return default

def _start_thread(target, name: str):
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t

# ── Core ops ───────────────────────────────────────────────────────────────────
def _safe_send(msg: str):
    try:
        if send_telegram:
            return send_telegram(msg)
        log.warning("Telegram not available: %r", msg)
        return False, "no-telegram"
    except Exception as e:
        log.exception("Telegram send failed: %s", e)
        return False, str(e)

def _safe_send_many(parts):
    if not parts:
        return
    if send_telegram_messages:
        try:
            return send_telegram_messages(parts)
        except Exception:
            log.exception("send_telegram_messages failed; falling back to singles")
    for p in parts:
        _safe_send(p)

def _snapshot():
    """Return latest wallet snapshot as dict; tolerate different signatures."""
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

# ── Reports ────────────────────────────────────────────────────────────────────
def _day_report_text(snap):
    if not build_day_report_text:
        return "No day report available."
    try:
        try:
            return build_day_report_text(snap)
        except TypeError:
            return build_day_report_text()
    except Exception:
        log.exception("build_day_report_text failed")
        return "Failed to build day report."

def _weekly_report_text():
    if not build_weekly_report_text:
        return "No weekly report available."
    try:
        return build_weekly_report_text()
    except Exception:
        log.exception("build_weekly_report_text failed")
        return "Failed to build weekly report."

# ── Workers ────────────────────────────────────────────────────────────────────
def _schedule_worker():
    """Drives both python-schedule and optional reports.scheduler.run_pending."""
    if not schedule and not scheduler_run_pending:
        log.error("No scheduler available; skipping schedule worker")
        return
    while True:
        try:
            if schedule:
                schedule.run_pending()
            if scheduler_run_pending:
                try:
                    scheduler_run_pending()
                except Exception:
                    log.exception("reports.scheduler.run_pending error")
        except Exception:
            log.exception("schedule worker error")
        time.sleep(1)

def _tg_longpoll_worker():
    if callable(telegram_long_poll_loop):
        try:
            telegram_long_poll_loop()
        except Exception:
            log.exception("telegram long-poll loop crashed")
    else:
        log.warning("telegram_long_poll_loop missing; skipping TG thread")

def _intraday_tick():
    """One cycle: snapshot -> day report -> send."""
    try:
        snap = _snapshot()
        txt  = _day_report_text(snap)
        if not txt:
            txt = "No updates."
        # Chunk if extremely long
        if len(txt) > 3500 and send_telegram_messages:
            # naive chunking on paragraphs
            parts, acc = [], []
            size = 0
            for line in txt.splitlines():
                size += len(line) + 1
                acc.append(line)
                if size > 3000:
                    parts.append("\n".join(acc)); acc=[]; size=0
            if acc: parts.append("\n".join(acc))
            _safe_send_many(parts)
        else:
            _safe_send("🕒 Intraday Update\n" + txt)
        try:
            note_tick()
        except Exception:
            pass
    except Exception:
        log.exception("intraday tick error")
        _safe_send("⚠️ runtime error (intraday)")

# ── Main ───────────────────────────────────────────────────────────────────────
def run():
    log.info("🟢 Starting Wallet Monitor")

    # TG long-poll
    _start_thread(_tg_longpoll_worker, "tg-long-poll")

    # Startup ping
    _safe_send("✅ Cronos DeFi Sentinel started and is online.")

    # Scheduling
    if schedule:
        EOD_TIME   = _env_str("EOD_TIME", "23:59")
        HEALTH_MIN = int(_env_float("HEALTH_MIN", 30))
        WEEKLY_DOW = _env_str("WEEKLY_DOW", "SUN").upper()
        WEEKLY_TIME= _env_str("WEEKLY_TIME", "18:00")

        # EOD day report
        try:
            schedule.every().day.at(EOD_TIME).do(lambda: (_safe_send("📒 Daily Report\n" + _day_report_text(_snapshot())), True))
            log.info("daily report scheduled at %s", EOD_TIME)
        except Exception:
            log.exception("bind EOD failed")

        # Weekly report (simple: fire every day at WEEKLY_TIME; guard inside for DOW)
        def _weekly_job():
            try:
                import datetime as _dt
                dow = _dt.datetime.now().strftime("%a").upper()  # e.g., 'SUN'
                if dow == WEEKLY_DOW:
                    _safe_send("🗓 Weekly Report\n" + _weekly_report_text())
            except Exception:
                log.exception("weekly job failed")
            return True
        try:
            schedule.every().day.at(WEEKLY_TIME).do(_weekly_job)
            log.info("weekly report scheduled every %s at %s", WEEKLY_DOW, WEEKLY_TIME)
        except Exception:
            log.exception("bind weekly failed")

        # Health ping
        try:
            schedule.every(HEALTH_MIN).minutes.do(lambda: (_safe_send("✅ alive"), True))
            log.info("health ping scheduled every %s minute(s)", HEALTH_MIN)
        except Exception:
            log.exception("bind health failed")

        # Start schedule worker
        _start_thread(_schedule_worker, "schedule-worker")
    else:
        log.error("python-schedule unavailable; timers disabled")

    # First tick then loop
    try:
        _intraday_tick()
    except Exception:
        log.exception("first tick failed")

    INTRADAY_HOURS = _env_float("INTRADAY_HOURS", 1.0)
    sleep_s = max(60, int(INTRADAY_HOURS * 3600))
    while True:
        _intraday_tick()
        log.info("sleeping %ss until next tick", sleep_s)
        time.sleep(sleep_s)

if __name__ == "__main__":
    run()
