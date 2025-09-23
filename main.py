#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (clean boot)
- Loads .env and applies env aliases
- Safe Telegram startup ping with MarkdownV2 escaping handled in telegram/api.py
- Daily report scheduler (EOD at EOD_TIME env or 23:59)
- Optional intraday holdings snapshot scheduler (INTRADAY_HOURS)
- On-demand holdings snapshot at startup (optional via STARTUP_SNAPSHOT=true)
- Minimal, dependency-light: relies only on existing repo modules

Required modules in repo:
  core/config.py         (apply_env_aliases, validate_env, get_env)
  core/holdings.py       (get_wallet_snapshot)
  telegram/api.py        (send_telegram_message)  -> handles MDV2 escaping & chunking
  telegram/formatters.py (format_holdings)
  reports/day_report.py  (build_day_report_text)

External deps:
  python-dotenv, schedule, requests

Environment (examples; see your Railway defaults):
  TZ=Europe/Athens
  TELEGRAM_BOT_TOKEN=*** (required)
  TELEGRAM_CHAT_ID=5307877340 (required)
  WALLET_ADDRESS=0x... (recommended)
  CRONOS_RPC_URL=https://cronos-evm-rpc.publicnode.com (default ok)
  EOD_TIME=23:59
  INTRADAY_HOURS=3
  STARTUP_SNAPSHOT=true
"""

from __future__ import annotations

import os
import sys
import time
import logging
import signal
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
from typing import Optional

# Third-party
from dotenv import load_dotenv  # type: ignore
import schedule  # type: ignore

# Repo modules (must exist)
from core.config import apply_env_aliases, validate_env, get_env
from telegram.api import send_telegram_message
from reports.day_report import build_day_report_text

# Optional (guarded)
try:
    from core.holdings import get_wallet_snapshot  # type: ignore
except Exception:  # pragma: no cover
    get_wallet_snapshot = None  # type: ignore

try:
    from telegram.formatters import format_holdings  # type: ignore
except Exception:  # pragma: no cover
    format_holdings = None  # type: ignore

# ---------- Precision ----------
getcontext().prec = 28

# ---------- Logging ----------
def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    fmt = os.getenv(
        "LOG_FORMAT",
        "%(asctime)s | %(levelname)s | %(message)s",
    )
    datefmt = "%Y-%m-%d %H:%M:%S"
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt)
    # Silence noisy libs if needed
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------- Helpers ----------
def _now_local() -> datetime:
    # Keep it simple: system TZ should be set via env TZ on Railway
    return datetime.now()

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def _get_eod_time() -> str:
    t = os.getenv("EOD_TIME", "23:59").strip()
    try:
        hh, mm = t.split(":")
        ih, im = int(hh), int(mm)
        if 0 <= ih <= 23 and 0 <= im <= 59:
            return f"{ih:02d}:{im:02d}"
    except Exception:
        pass
    logging.warning("Invalid EOD_TIME '%s', falling back to 23:59", t)
    return "23:59"

def _get_intraday_hours() -> Optional[int]:
    v = os.getenv("INTRADAY_HOURS", "").strip()
    if not v:
        return None
    try:
        iv = int(v)
        if iv > 0:
            return iv
    except Exception:
        pass
    logging.warning("Invalid INTRADAY_HOURS '%s' (ignored)", v)
    return None

def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"


# ---------- Telegram tasks ----------
def send_startup_ping() -> None:
    try:
        send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")
    except Exception as e:  # pragma: no cover
        logging.exception("Telegram startup ping failed: %s", e)

def send_daily_report() -> None:
    try:
        text = build_day_report_text()
        send_telegram_message("📒 Daily Report\n" + text)
    except Exception as e:  # pragma: no cover
        logging.exception("Failed to build/send daily report: %s", e)
        try:
            send_telegram_message("⚠️ Failed to generate daily report.")
        except Exception:
            pass

def send_holdings_snapshot() -> None:
    if get_wallet_snapshot is None or format_holdings is None:
        logging.warning("Holdings snapshot not available (missing modules).")
        return
    try:
        wallet = os.getenv("WALLET_ADDRESS", "").strip()
        snap = None
        try:
            # Prefer explicit wallet address if your helper accepts it;
            # if signature differs, fallback without args.
            if wallet:
                snap = get_wallet_snapshot(wallet)  # type: ignore[arg-type]
            else:
                snap = get_wallet_snapshot()  # type: ignore[call-arg]
        except TypeError:
            # Signature mismatch: retry without args
            snap = get_wallet_snapshot()  # type: ignore[call-arg]

        txt = None
        try:
            txt = format_holdings(snap)  # type: ignore[call-arg]
        except TypeError:
            # Some formatters accept kwargs
            txt = format_holdings(snapshot=snap)  # type: ignore[call-arg]

        if not txt:
            txt = "Holdings snapshot is empty."
        send_telegram_message("🧾 Holdings Snapshot\n" + txt)
    except Exception as e:  # pragma: no cover
        logging.exception("Failed to build/send holdings snapshot: %s", e)
        try:
            send_telegram_message("⚠️ Failed to build holdings snapshot.")
        except Exception:
            pass


# ---------- Scheduler loop ----------
def _bind_schedules() -> None:
    # Daily report at EOD
    eod = _get_eod_time()
    schedule.every().day.at(eod).do(send_daily_report)
    logging.info("Scheduled daily report at %s", eod)

    # Intraday holdings snapshot (optional)
    ih = _get_intraday_hours()
    if ih:
        schedule.every(ih).hours.do(send_holdings_snapshot)
        logging.info("Scheduled intraday holdings snapshot every %s hour(s)", ih)

def scheduler_run_loop(stop_flag: list[bool]) -> None:
    last_tick = time.time()
    while not stop_flag[0]:
        try:
            schedule.run_pending()
        except Exception as e:  # pragma: no cover
            logging.exception("schedule.run_pending() failed: %s", e)
        time.sleep(1)
        if time.time() - last_tick >= 60:
            last_tick = time.time()
            logging.debug("Scheduler heartbeat ok.")


# ---------- Signal handling ----------
def _install_signal_handlers(stop_flag: list[bool]) -> None:
    def _graceful(sig, frame):  # pragma: no cover
        logging.info("Received signal %s, stopping...", sig)
        stop_flag[0] = True
        try:
            send_telegram_message("🛑 Cronos DeFi Sentinel stopping...")
        except Exception:
            pass

    try:
        signal.signal(signal.SIGINT, _graceful)
        signal.signal(signal.SIGTERM, _graceful)
    except Exception:
        # Some environments may not allow signal handlers
        pass


# ---------- Main ----------
def main() -> int:
    setup_logging()
    logging.info("Starting Cronos DeFi Sentinel...")

    # Load env and normalize
    try:
        load_dotenv(override=False)
    except Exception:
        pass
    apply_env_aliases()
    validate_env(strict=False)

    # Startup ping
    send_startup_ping()

    # Optional: holdings snapshot at boot
    if _env_bool("STARTUP_SNAPSHOT", True):
        send_holdings_snapshot()

    # Bind schedules
    _bind_schedules()

    # Run loop
    stop_flag = [False]
    _install_signal_handlers(stop_flag)
    t0 = time.time()
    try:
        scheduler_run_loop(stop_flag)
    except Exception as e:  # pragma: no cover
        logging.exception("Fatal in scheduler loop: %s", e)
        try:
            send_telegram_message("❌ Fatal error in main loop.")
        except Exception:
            pass
        return 1
    finally:
        dt = _fmt_duration(time.time() - t0)
        logging.info("Exited main loop after %s", dt)

    logging.info("Bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
