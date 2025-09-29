# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — modular main.py (orchestrator)

Responsibilities:
- Bootstrap: dotenv, TZ, logging, env aliases
- Startup signal to Telegram
- Seed/refresh in-memory holdings state for guards, reports, commands
- Start background subsystems (watch/discovery, wallet monitor, scheduler, signals server)
- Keep-alive & graceful shutdown

This main relies on modular components exposed by your codebase:
  reports.scheduler:      start_eod_scheduler(), run_pending()
  telegram.api:           send_telegram_message()
  telegram.dispatcher:    dispatch()                      # used optionally for simple self-test
  core.watch:             make_from_env()                 # returns a watch/discovery runner
  core.wallet_monitor:    make_wallet_monitor()           # returns a wallet monitor runner
  core.providers.cronos:  fetch_wallet_txs                # optional sanity/self-test
  core.signals.server:    start_signals_server_if_enabled # starts HTTP server if configured
  core.holdings:          get_wallet_snapshot             # canonical snapshot provider
  core.guards:            set_holdings                    # publish snapshot to guards subsystem
"""

import os
import sys
import time
import json
import signal
import logging
import threading
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# ============ External, modular APIs ============
# (Intentionally minimal; if any is missing at runtime we log a warning and continue)
try:
    from reports.scheduler import start_eod_scheduler, run_pending
except Exception as _e:
    start_eod_scheduler = None   # type: ignore
    run_pending = None           # type: ignore

try:
    from telegram.api import send_telegram_message
except Exception as _e:
    def send_telegram_message(msg: str) -> None:  # fallback no-op
        logging.getLogger("main").warning("telegram.api.send_telegram_message unavailable; message not sent: %s", msg)

try:
    from telegram.dispatcher import dispatch
except Exception:
    dispatch = None  # type: ignore

try:
    from core.watch import make_from_env as make_watch_from_env
except Exception:
    make_watch_from_env = None  # type: ignore

try:
    from core.wallet_monitor import make_wallet_monitor
except Exception:
    make_wallet_monitor = None  # type: ignore

try:
    from core.providers.cronos import fetch_wallet_txs
except Exception:
    fetch_wallet_txs = None  # type: ignore

try:
    from core.signals.server import start_signals_server_if_enabled
except Exception:
    start_signals_server_if_enabled = None  # type: ignore

try:
    from core.holdings import get_wallet_snapshot
except Exception:
    get_wallet_snapshot = None  # type: ignore

try:
    from core.guards import set_holdings
except Exception:
    set_holdings = None  # type: ignore

# ============ Bootstrap ============
load_dotenv()

def _alias_env(src: str, dst: str) -> None:
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)  # type: ignore

# Back-compat aliases used across the project
_alias_env("ALERTS_INTERVAL_MINUTES", "ALERTS_INTERVAL_MIN")
_alias_env("DISCOVER_REQUIRE_WCRO_QUOTE", "DISCOVER_REQUIRE_WCRO")

def _init_tz(tz_str: Optional[str]) -> ZoneInfo:
    tz = tz_str or "Europe/Athens"
    os.environ["TZ"] = tz
    try:
        if hasattr(time, "tzset"):
            time.tzset()  # type: ignore[attr-defined]
    except Exception:
        pass
    return ZoneInfo(tz)

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = _init_tz(TZ)

def now_dt() -> datetime:
    return datetime.now(LOCAL_TZ)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

# ============ Globals ============
_shutdown = threading.Event()

# ============ Threads helpers ============
def _start_thread(target, name: str) -> None:
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    log.info("Thread started: %s", name)

# ============ Subsystem wrappers ============
def _seed_holdings_once() -> None:
    """Pull current wallet snapshot and publish to guards (if both available)."""
    try:
        if get_wallet_snapshot is None or set_holdings is None:
            log.warning("Holdings seed skipped (get_wallet_snapshot/set_holdings unavailable).")
            return
        snapshot = get_wallet_snapshot()
        set_holdings(snapshot)
        log.info("Holdings snapshot seeded to guards.")
    except Exception as e:
        log.exception("Failed to seed holdings: %s", e)

def _run_scheduler_loop() -> None:
    """Kick off EOD schedule and run pending in a lightweight loop."""
    try:
        if start_eod_scheduler is not None:
            start_eod_scheduler()  # sets up daily EOD job(s) based on env
            log.info("EOD scheduler initialized.")
        else:
            log.warning("reports.scheduler.start_eod_scheduler unavailable.")
    except Exception as e:
        log.exception("start_eod_scheduler failed: %s", e)

    while not _shutdown.is_set():
        try:
            if run_pending is not None:
                run_pending()  # execute due jobs
            else:
                # If scheduler not present, idle quietly
                time.sleep(1)
        except Exception as e:
            log.exception("Scheduler loop error: %s", e)
        finally:
            # Keep the loop light; most schedulers expect sub-1s to a few secs cadence
            time.sleep(1)

def _run_watch_loop() -> None:
    """Start the Dex discovery/watch subsystem from env."""
    if make_watch_from_env is None:
        log.warning("core.watch.make_from_env unavailable; watch loop not started.")
        return
    try:
        runner = make_watch_from_env()
        # Prefer a unified `.run()` or `.start()` contract.
        if hasattr(runner, "start"):
            runner.start()
        elif hasattr(runner, "run"):
            runner.run()
        else:
            # last resort: call as a function if callable
            if callable(runner):
                runner()
            else:
                log.warning("Watch runner has no start/run/callable interface.")
        # If the runner is blocking, it will stay here until shutdown signal
        while not _shutdown.is_set():
            time.sleep(1)
    except Exception as e:
        log.exception("Watch loop failed: %s", e)

def _run_wallet_monitor_loop() -> None:
    """Start the wallet monitor subsystem."""
    if make_wallet_monitor is None:
        log.warning("core.wallet_monitor.make_wallet_monitor unavailable; wallet monitor not started.")
        return
    try:
        mon = make_wallet_monitor()
        if hasattr(mon, "start"):
            mon.start()
        elif hasattr(mon, "run"):
            mon.run()
        else:
            if callable(mon):
                mon()
            else:
                log.warning("Wallet monitor has no start/run/callable interface.")
        while not _shutdown.is_set():
            time.sleep(1)
    except Exception as e:
        log.exception("Wallet monitor loop failed: %s", e)

def _start_signals_server() -> None:
    """Start optional HTTP signals server if enabled via env/config."""
    try:
        if start_signals_server_if_enabled is not None:
            start_signals_server_if_enabled()
            log.info("Signals server checked/started (if enabled).")
        else:
            log.info("Signals server module not available; skipping.")
    except Exception as e:
        log.exception("Signals server failed to start: %s", e)

# ============ Optional self-tests ============
def _selftest_log_env() -> None:
    """Very light diagnostics to help early failures surface in logs."""
    wal = (os.getenv("WALLET_ADDRESS") or "").lower()
    rpc = bool(os.getenv("CRONOS_RPC_URL"))
    eth = bool(os.getenv("ETHERSCAN_API"))
    chat = str(os.getenv("TELEGRAM_CHAT_ID") or "")
    log.info("Env check — WALLET_ADDRESS set: %s | CRONOS_RPC_URL set: %s | ETHERSCAN_API set: %s | TELEGRAM_CHAT_ID: %s",
             bool(wal), rpc, eth, chat if chat else "(unset)")

def _selftest_ping_cronos() -> None:
    """Optional lightweight check that provider wiring works."""
    if fetch_wallet_txs is None:
        return
    try:
        txs = fetch_wallet_txs(limit=1)  # type: ignore[call-arg]
        if isinstance(txs, list):
            log.info("Cronos provider sanity: OK (txs=%d).", len(txs))
    except Exception as e:
        log.warning("Cronos provider sanity failed: %s", e)

# ============ Shutdown ============
def _graceful_exit(signum, frame) -> None:
    try:
        send_telegram_message("🛑 Shutting down.")
    except Exception:
        pass
    _shutdown.set()

# ============ Main ============
def main() -> None:
    _selftest_log_env()

    # Startup message (per your rule/memory)
    try:
        send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")
    except Exception as e:
        log.warning("Startup Telegram message failed: %s", e)

    # Seed holdings to guards (so GUARD / reports have baseline)
    _seed_holdings_once()

    # Start optional HTTP signals server (if enabled)
    _start_signals_server()

    # Start subsystems
    _start_thread(_run_watch_loop,          "watch")
    _start_thread(_run_wallet_monitor_loop, "wallet-monitor")
    _start_thread(_run_scheduler_loop,      "scheduler")

    # Optional: tiny provider sanity (logs only)
    _selftest_ping_cronos()

    # Keep-alive
    while not _shutdown.is_set():
        time.sleep(1)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log = logging.getLogger("main")
        log.exception("Fatal error: %s", e)
        try:
            send_telegram_message(f"💥 Fatal error: {e}")
        except Exception:
            pass
        sys.exit(1)
