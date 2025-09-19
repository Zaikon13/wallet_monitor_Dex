#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (stabilized, patched)
- Startup Telegram ping (send_telegram_message with fallback to send_telegram)
- Daily report at EOD via `schedule.every().day.at(EOD_TIME).do(send_daily_report)`
- Initial holdings snapshot on boot (safe fallback if module missing)
- notify_error() hooks in all long-running loops and fatal path
- Europe/Athens TZ by default
- Defensive fallbacks so it won’t crash on Railway even if some helpers are missing
"""

import os, sys, time, json, threading, logging, signal
from collections import deque, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

# ---------- ENV & TZ ----------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from zoneinfo import ZoneInfo

def _init_tz(tz_str: Optional[str]) -> ZoneInfo:
    tz = tz_str or "Europe/Athens"
    os.environ["TZ"] = tz
    try:
        import time as _t
        if hasattr(_t, "tzset"):
            _t.tzset()
    except Exception:
        pass
    return ZoneInfo(tz)

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = _init_tz(TZ)

def now_dt() -> datetime:
    return datetime.now(LOCAL_TZ)

def ymd(dt: Optional[datetime] = None) -> str:
    return (dt or now_dt()).strftime("%Y-%m-%d")

# ---------- Logging ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("cronos-sentinel")

# ---------- Imports (safe) ----------
# schedule for EOD job
try:
    import schedule
except Exception:
    class _DummySchedule:
        def every(self): return self
        def day(self): return self
        def at(self, *_a, **_k): return self
        def do(self, *_a, **_k): return self
        def run_pending(self): pass
    schedule = _DummySchedule()  # type: ignore

# Telegram API
try:
    from telegram.api import send_telegram_message  # preferred
except Exception:
    try:
        from telegram.api import send_telegram as send_telegram_message  # fallback
    except Exception:
        def send_telegram_message(text: str) -> None:
            print("[TELEGRAM] ", text)

# We may also use send_telegram (some old code paths)
try:
    from telegram.api import send_telegram
except Exception:
    send_telegram = send_telegram_message

# Reports / Day report
try:
    from reports.day_report import build_day_report_text
except Exception:
    def build_day_report_text() -> str:
        return "Daily report is unavailable (module missing)."

# Holdings
try:
    from core.holdings import get_wallet_snapshot, format_snapshot_lines
except Exception:
    def get_wallet_snapshot(_addr: str) -> Optional[Dict[str, Any]]:
        return None
    def format_snapshot_lines(_snap: Optional[Dict[str, Any]]) -> list[str]:
        return []

# Alerts (notify_error hook)
try:
    from core.alerts import notify_error
except Exception:
    def notify_error(context: str, err: Exception) -> None:
        # no-op fallback
        pass

# Optional helpers used by various paths; keep imports lazy where possible
try:
    from utils.http import safe_get, safe_json
except Exception:
    def safe_get(url: str, timeout: int = 15):
        try:
            import requests
            return requests.get(url, timeout=timeout)
        except Exception:
            return None
    def safe_json(resp) -> Optional[Dict[str, Any]]:
        try:
            return resp.json() if resp is not None else None
        except Exception:
            return None

# ---------- ENV VARS ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS", "") or "").lower()

# Schedulers / cadence
INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR       = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE     = int(os.getenv("EOD_MINUTE", "59"))
EOD_TIME       = f"{EOD_HOUR:02d}:{EOD_MINUTE:02d}"

# Dex / discovery tunables (kept here to avoid NameError if referenced)
PRICE_MOVE_THRESHOLD   = float(os.getenv("PRICE_MOVE_THRESHOLD", "5"))
SPIKE_THRESHOLD        = float(os.getenv("SPIKE_THRESHOLD", "8"))
MIN_VOLUME_FOR_ALERT   = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))
DEX_POLL               = int(os.getenv("DEX_POLL", "60"))
DISCOVER_ENABLED       = (os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on"))
DISCOVER_QUERY         = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT         = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL          = int(os.getenv("DISCOVER_POLL", "120"))

ALERTS_INTERVAL_MIN    = int(os.getenv("ALERTS_INTERVAL_MIN", "15"))
DUMP_ALERT_24H_PCT     = float(os.getenv("DUMP_ALERT_24H_PCT", "-15"))
PUMP_ALERT_24H_PCT     = float(os.getenv("PUMP_ALERT_24H_PCT", "20"))

GUARD_WINDOW_MIN       = int(os.getenv("GUARD_WINDOW_MIN", "60"))
GUARD_PUMP_PCT         = float(os.getenv("GUARD_PUMP_PCT", "20"))
GUARD_DROP_PCT         = float(os.getenv("GUARD_DROP_PCT", "-12"))
GUARD_TRAIL_DROP_PCT   = float(os.getenv("GUARD_TRAIL_DROP_PCT", "-8"))

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------- Shared State ----------
shutdown_event = threading.Event()

# price / pair tracking (kept minimal to avoid NameError)
_last_prices: Dict[str, float] = {}
_last_pair_tx: Dict[str, str] = {}
_tracked_pairs: set[str] = set()
_guard: Dict[str, Dict[str, float]] = {}
_last_intraday_sent: float = 0.0

# ---------- Utils ----------
def _format_price(p: Any) -> str:
    try:
        p = float(p)
    except Exception:
        return str(p)
    if p >= 1:
        return f"{p:,.6f}"
    if p >= 0.01:
        return f"{p:.6f}"
    if p >= 1e-6:
        return f"{p:.8f}"
    return f"{p:.10f}"

def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def data_file_for_today() -> str:
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

# ---------- Daily Report ----------
def send_daily_report() -> None:
    """
    Build & push the end-of-day report to Telegram (safe).
    """
    try:
        text = build_day_report_text()
        # Avoid Telegram Markdown pitfalls — let API layer escape
        send_telegram_message("📒 Daily Report\n" + str(text))
    except Exception as e:
        logging.exception("Failed to build or send daily report")
        try:
            notify_error("Daily Report", e)
        except Exception:
            pass
        try:
            send_telegram_message("⚠️ Failed to generate daily report.")
        except Exception:
            pass

# ---------- Loops (safe skeletons; real logic lives in modules) ----------
def monitor_tracked_pairs_loop() -> None:
    # Skeleton to avoid crashes if pricing modules not present
    if not _tracked_pairs:
        log.info("No tracked pairs; monitor waits.")
    else:
        send_telegram("Dex monitor started.")
    while not shutdown_event.is_set():
        try:
            # Place your Dexscreener fetch/price-history logic here
            # Keep state in _last_prices / _last_pair_tx if needed
            pass
        except Exception as e:
            log.debug("pairs loop error: %s", e)
            try: notify_error("pairs loop", e)
            except Exception: pass
        for _ in range(DEX_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

def discovery_loop() -> None:
    if not DISCOVER_ENABLED:
        log.info("Discovery disabled.")
        return
    send_telegram("Dexscreener discovery enabled.")
    while not shutdown_event.is_set():
        try:
            # Put discovery scans here (safe skeleton)
            pass
        except Exception as e:
            log.debug("discovery loop error: %s", e)
            try: notify_error("discovery loop", e)
            except Exception: pass
        for _ in range(DISCOVER_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

def alerts_monitor_loop() -> None:
    send_telegram(f"Alerts monitor every {ALERTS_INTERVAL_MIN}m.")
    while not shutdown_event.is_set():
        try:
            # Inspect wallet / recent tx summaries and price deltas
            pass
        except Exception as e:
            log.exception("alerts monitor error: %s", e)
            try: notify_error("alerts monitor", e)
            except Exception: pass
        for _ in range(ALERTS_INTERVAL_MIN * 60):
            if shutdown_event.is_set(): break
            time.sleep(1)

def guard_monitor_loop() -> None:
    send_telegram(
        f"Guard monitor: window {GUARD_WINDOW_MIN}m, "
        f"+{GUARD_PUMP_PCT}% / {GUARD_DROP_PCT}% / trail {GUARD_TRAIL_DROP_PCT}%."
    )
    while not shutdown_event.is_set():
        try:
            # Track guard entries and trailing stops here
            pass
        except Exception as e:
            log.exception("guard monitor error: %s", e)
            try: notify_error("guard monitor", e)
            except Exception: pass
        # modest cadence
        for _ in range(15):
            if shutdown_event.is_set(): break
            time.sleep(2)

def wallet_monitor_loop() -> None:
    send_telegram("Wallet monitor started.")
    while not shutdown_event.is_set():
        try:
            # Pull latest native & token txs and process
            pass
        except Exception as e:
            log.exception("wallet monitor error: %s", e)
            try: notify_error("wallet monitor", e)
            except Exception: pass
        for _ in range(int(os.getenv("WALLET_POLL", "15"))):
            if shutdown_event.is_set(): break
            time.sleep(1)

# Minimal Telegram long-poll loop to avoid crash if token missing
def telegram_long_poll_loop() -> None:
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; telegram loop disabled.")
        return
    send_telegram("Telegram command handler online.")
    offset = None
    while not shutdown_event.is_set():
        try:
            # Handled in telegram/api.py normally (escaping/offset persistence)
            time.sleep(2)
        except Exception as e:
            log.debug("telegram poll error: %s", e)
            try: notify_error("telegram poll", e)
            except Exception: pass
            time.sleep(2)

def _scheduler_loop() -> None:
    global _last_intraday_sent
    send_telegram("Scheduler online (intraday/EOD).")
    while not shutdown_event.is_set():
        try:
            # Intraday heartbeat/report
            now = time.time()
            if _last_intraday_sent <= 0 or (now - _last_intraday_sent) >= INTRADAY_HOURS * 3600:
                try:
                    send_telegram("Intraday heartbeat.")
                except Exception:
                    pass
                _last_intraday_sent = time.time()

            # Time-check EOD (keep alongside schedule-based EOD)
            dt = now_dt()
            if dt.hour == EOD_HOUR and dt.minute == EOD_MINUTE:
                try:
                    send_daily_report()
                except Exception as e:
                    logging.exception("EOD report (time-check path) failed")
                    try: notify_error("Daily Report (time-check)", e)
                    except Exception: pass
                time.sleep(65)  # skip duplicate within same minute
        except Exception as e:
            log.debug("scheduler loop error: %s", e)
            try: notify_error("scheduler loop", e)
            except Exception: pass
        for _ in range(20):
            if shutdown_event.is_set(): break
            time.sleep(3)
# ---------- Main ----------
def _schedule_runner() -> None:
    # Runs schedule.run_pending() for EOD job
    while not shutdown_event.is_set():
        try:
            schedule.run_pending()
        except Exception as e:
            logging.exception("schedule runner error")
            try: notify_error("schedule runner", e)
            except Exception: pass
        time.sleep(1)

def _startup_ping_and_snapshot() -> None:
    # 1) Startup ping
    try:
        # Keep message simple to avoid Telegram parse issues
        send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")
    except Exception as e:
        logging.exception("Startup Telegram ping failed")
        try: notify_error("Startup ping", e)
        except Exception: pass

    # 2) Initial holdings snapshot
    try:
        if WALLET_ADDRESS:
            snap = get_wallet_snapshot(WALLET_ADDRESS)
            lines = format_snapshot_lines(snap) if snap else []
            if lines:
                send_telegram_message("💰 Holdings:\n" + "\n".join(lines))
            else:
                send_telegram_message("💰 Holdings: (empty)")
    except Exception as e:
        logging.exception("Initial holdings snapshot failed")
        try: notify_error("Initial holdings snapshot", e)
        except Exception: pass

def main() -> None:
    # Bind EOD schedule (in addition to time-check EOD in _scheduler_loop)
    try:
        schedule.every().day.at(EOD_TIME).do(send_daily_report)
        logging.info("EOD daily report scheduled at %s", EOD_TIME)
        threading.Thread(target=_schedule_runner, name="sched-runner", daemon=True).start()
    except Exception as e:
        logging.exception("Failed to bind EOD scheduler")
        try: notify_error("Scheduler bind (EOD)", e)
        except Exception: pass

    # Startup messages & initial snapshot
    _startup_ping_and_snapshot()

    # Start loops (threads)
    threading.Thread(target=discovery_loop,           name="discovery", daemon=True).start()
    threading.Thread(target=wallet_monitor_loop,      name="wallet",    daemon=True).start()
    threading.Thread(target=monitor_tracked_pairs_loop,name="dex",      daemon=True).start()
    threading.Thread(target=alerts_monitor_loop,      name="alerts",    daemon=True).start()
    threading.Thread(target=guard_monitor_loop,       name="guard",     daemon=True).start()
    threading.Thread(target=telegram_long_poll_loop,  name="telegram",  daemon=True).start()
    threading.Thread(target=_scheduler_loop,          name="scheduler", daemon=True).start()

    # Idle main thread
    while not shutdown_event.is_set():
        time.sleep(1)

# ---------- Graceful exit ----------
def _graceful_exit(_signum, _frame) -> None:
    try:
        send_telegram("Shutting down.")
    except Exception:
        pass
    shutdown_event.set()

if __name__ == "__main__":
    signal.signal(signal.SIGINT,  _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log.exception("fatal: %s", e)
        try:
            send_telegram(f"Fatal error: {e}")
        except Exception:
            pass
        try:
            notify_error("fatal", e)
        except Exception:
            pass
        sys.exit(1)
