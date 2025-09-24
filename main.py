#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (stabilized, modular, aligned to current repo)

- Uses existing modules exactly as present in the repo:
  core.config, core.rpc, core.pricing, core.holdings, core.alerts, core.tz
  reports.day_report, reports.aggregates, reports.ledger
  telegram.api, telegram.formatters, telegram.dispatcher, telegram.commands
  utils.http

- Startup: applies env aliases, validates env (with local fallback if core.config.validate_env is missing),
  configures logging, sends a single Telegram "online" message.

- Concurrency: thread-safe shared dictionaries via RLock for price state; EOD scheduler runs safely.
- Alerts: uses core.alerts.notify_*; fixes price-delta logic (read prev before write).
- EOD: imports build_day_report_text and sends daily report at configured EOD_TIME.

Assumptions:
- Repo structure matches the tree you provided; no new files introduced.
- This file avoids making breaking changes to other modules. Where a function might not exist yet
  (e.g., core.config.validate_env), a local fallback is used so the program still boots cleanly.
"""

from __future__ import annotations

import os
import sys
import json
import time
import logging
import threading
from decimal import Decimal
from typing import Dict, Any, Optional, Tuple, List

# Third-party
try:
    import schedule  # type: ignore
except Exception:
    schedule = None  # Scheduler is optional; EOD task will be skipped if missing

# --- Repo modules (as-is) ---
from core.config import apply_env_aliases
try:
    from core.config import validate_env as _validate_env  # may be missing in current repo
except Exception:
    _validate_env = None

from core import rpc as core_rpc
from core import pricing as core_pricing
from core import holdings as core_holdings
from core import tz as core_tz

from core.alerts import notify_error, notify_alert

from reports.day_report import build_day_report_text

from telegram.api import send_telegram_message
# Commands/dispatcher are present but polling loop is assumed elsewhere;
# this main focuses on schedulers + monitoring loops integration.
# from telegram.dispatcher import register_handlers  # if needed
# from telegram.commands import ...  # handlers used by dispatcher

# --- Constants / Env ---
APP_NAME = "Cronos DeFi Sentinel"
DEFAULT_TZ = os.getenv("TZ", "Europe/Athens")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# EOD_TIME may exist in env defaults memory; fallback to "23:59"
EOD_TIME = os.getenv("EOD_TIME", "23:59")

# Alert thresholds (keep existing names; read if present)
PRICE_MOVE_THRESHOLD = Decimal(str(os.getenv("PRICE_MOVE_THRESHOLD", "5")))  # pct
GUARD_WINDOW_MIN = int(os.getenv("GUARD_WINDOW_MIN", os.getenv("GUARD_WINDOW_MINUTES", "60")))

# Poll intervals
WALLET_POLL_SEC = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL_SEC = int(os.getenv("DEX_POLL", "60"))

# Logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- Logging setup ---
def setup_logging() -> None:
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --- Local fallback for validate_env (if missing in core.config) ---
def _fallback_validate_env() -> None:
    """
    Minimal env validation if core.config.validate_env is absent.
    Required: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WALLET_ADDRESS, CRONOS_RPC_URL
    """
    required = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "WALLET_ADDRESS",
        "CRONOS_RPC_URL",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        msg = f"Missing required environment variables: {', '.join(missing)}"
        logging.error(msg)
        # Best-effort notification (if token/chat present)
        try:
            send_telegram_message(f"⚠️ {APP_NAME} failed to start: {msg}")
        except Exception:
            pass
        raise SystemExit(2)


def validate_env() -> None:
    if _validate_env is not None:
        _validate_env()
    else:
        _fallback_validate_env()


# --- Thread-safe shared state for prices ---
_prices_lock = threading.RLock()
_last_prices: Dict[str, Decimal] = {}          # symbol -> last seen price
_price_history: Dict[str, List[Tuple[float, Decimal]]] = {}  # symbol -> [(epoch, price), ...]

def _d(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")


# --- Price utilities ---
def get_symbol_price_usd(symbol: str) -> Optional[Decimal]:
    """
    Use core.pricing.get_price_usd with graceful handling.
    """
    try:
        p = core_pricing.get_price_usd(symbol)
        if p is None:
            return None
        return _d(p)
    except Exception as e:
        notify_error("pricing.get_price_usd", e)
        return None


def update_price_state(symbol: str, price: Decimal, ts: Optional[float] = None) -> Optional[Decimal]:
    """
    Update price history safely and return % change from previous if available.
    Fixes the regression: compute delta BEFORE writing the new price.
    """
    if ts is None:
        ts = time.time()
    with _prices_lock:
        prev = _last_prices.get(symbol)
        # Update history
        hist = _price_history.setdefault(symbol, [])
        hist.append((ts, price))
        # Compute delta BEFORE overwrite
        pct = None
        if prev is not None and prev > 0:
            try:
                pct = (price - prev) / prev * Decimal("100")
            except Exception:
                pct = None
        # Write the new price after computing delta
        _last_prices[symbol] = price
        return pct


def maybe_alert_price_move(symbol: str, pct_move: Optional[Decimal]) -> None:
    """
    If pct_move crosses threshold, send alert. Uses PRICE_MOVE_THRESHOLD from env.
    """
    if pct_move is None:
        return
    try:
        thr = PRICE_MOVE_THRESHOLD
    except Exception:
        thr = Decimal("5")
    try:
        if pct_move >= thr:
            notify_alert(f"{symbol} pumped {pct_move:.2f}%")
        elif pct_move <= -thr:
            notify_alert(f"{symbol} dumped {pct_move:.2f}%")
    except Exception as e:
        notify_error("maybe_alert_price_move", e)


# --- Snapshot / Holdings ---
def get_holdings_snapshot() -> Dict[str, Dict[str, Any]]:
    """
    Delegates to core.holdings.get_wallet_snapshot() if available.
    Ensures CRO vs tCRO are not merged; core.holdings already respects this invariant.
    """
    try:
        snap = core_holdings.get_wallet_snapshot()
        return snap or {}
    except Exception as e:
        notify_error("holdings.get_wallet_snapshot", e)
        return {}


# --- EOD report task ---
def send_daily_report() -> None:
    """
    Build and send the daily report via Telegram.
    Fully wrapped with exception handling; never raises.
    """
    try:
        text = build_day_report_text()
        # Build function should return plain text; telegram.api will escape as needed
        send_telegram_message(f"📒 Daily Report\n{text}")
    except Exception as e:
        logging.exception("Failed to build or send daily report")
        try:
            send_telegram_message("⚠️ Failed to generate daily report.")
        except Exception:
            pass
        notify_error("send_daily_report", e)


# --- Wallet monitor loop (lightweight placeholder using current modules) ---
def wallet_monitor_loop(stop_event: threading.Event) -> None:
    """
    Lightweight loop:
    - polls wallet snapshot for primary symbols
    - fetches prices and updates price state
    - triggers price move alerts when threshold is crossed

    Uses only repo-present helpers to avoid cross-file API mismatches.
    """
    symbols_to_track: List[str] = []
    last_symbol_scan = 0.0

    while not stop_event.is_set():
        t0 = time.time()
        try:
            # Refresh tracked symbols every 60s based on current snapshot
            if t0 - last_symbol_scan > 60:
                snapshot = get_holdings_snapshot()
                symbols_to_track = sorted(list(snapshot.keys()))
                last_symbol_scan = t0

            # Update prices for tracked symbols
            for sym in symbols_to_track:
                p = get_symbol_price_usd(sym)
                if p is None:
                    continue
                pct = update_price_state(sym, p, ts=t0)
                maybe_alert_price_move(sym, pct)

        except Exception as e:
            notify_error("wallet_monitor_loop", e)

        # sleep
        time.sleep(max(1, WALLET_POLL_SEC))


# --- Scheduler thread ---
def scheduler_loop(stop_event: threading.Event) -> None:
    """
    Runs schedule.run_pending() periodically if 'schedule' is available.
    """
    if schedule is None:
        logging.warning("schedule module not available; EOD task disabled.")
        return

    # Bind EOD job once at startup
    try:
        schedule.clear("eod")
    except Exception:
        pass
    try:
        schedule.every().day.at(EOD_TIME).do(send_daily_report).tag("eod")
        logging.info(f"EOD daily report scheduled at {EOD_TIME}")
    except Exception as e:
        notify_error("scheduler_bind_eod", e)

    while not stop_event.is_set():
        try:
            schedule.run_pending()
        except Exception as e:
            notify_error("scheduler.run_pending", e)
        time.sleep(1)


# --- Startup helpers ---
def _send_startup_ping() -> None:
    try:
        send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")
    except Exception as e:
        logging.warning(f"Telegram startup message failed: {e}")


def bootstrap() -> Tuple[threading.Event, List[threading.Thread]]:
    """
    Boot sequence:
    - Apply env aliases
    - Validate env (with fallback)
    - Config logging
    - Startup ping
    - Start background threads (wallet monitor, scheduler)
    """
    apply_env_aliases()
    validate_env()
    setup_logging()

    logging.info(f"Starting {APP_NAME}")
    _send_startup_ping()

    stop_event = threading.Event()
    threads: List[threading.Thread] = []

    # Wallet monitor
    t_wallet = threading.Thread(
        target=wallet_monitor_loop, args=(stop_event,), name="wallet-monitor", daemon=True
    )
    t_wallet.start()
    threads.append(t_wallet)

    # Scheduler
    t_sched = threading.Thread(
        target=scheduler_loop, args=(stop_event,), name="scheduler", daemon=True
    )
    t_sched.start()
    threads.append(t_sched)

    return stop_event, threads


def main() -> None:
    stop_event, threads = bootstrap()
    try:
        # keep main thread alive
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        logging.info("Shutdown requested, stopping threads...")
        stop_event.set()
        for t in threads:
            try:
                t.join(timeout=5.0)
            except Exception:
                pass
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
