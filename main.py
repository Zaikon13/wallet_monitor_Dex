#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Slim, compatibility-first main.py for wallet_monitor_Dex

What it does now:
- Loads .env and sets TZ (Europe/Athens default)
- Bridges repo inconsistencies:
  * telegram.api: add send_telegram_message alias to send_telegram
  * utils.http  : add get_json wrapper using safe_get + safe_json
- Starts:
  * Telegram long-poll command handler (uses telegram.dispatcher.dispatch)
  * Reports scheduler (daily & intraday) via reports.scheduler

What it doesn't do yet (we can enable next):
- Core wallet monitor / discovery loops (need minor fixes in core/*)
"""

import os
import sys
import time
import json
import signal
import logging
import threading
from typing import Optional

from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# --- Logging ---------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

# --- Env / TZ --------------------------------------------------------------
load_dotenv()

TZ = os.getenv("TZ", "Europe/Athens")
def _init_tz(tz_str: str) -> ZoneInfo:
    os.environ["TZ"] = tz_str
    try:
        import time as _t
        if hasattr(_t, "tzset"):
            _t.tzset()
    except Exception:
        pass
    return ZoneInfo(tz_str)

LOCAL_TZ = _init_tz(TZ)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""

# --- Compatibility bridges -------------------------------------------------
# 1) telegram.api: ensure send_telegram_message exists
try:
    from telegram import api as _tg_api
except Exception as e:
    log.error("Failed to import telegram.api: %s", e)
    _tg_api = None

if _tg_api:
    # Older core modules expect 'send_telegram_message'
    if not hasattr(_tg_api, "send_telegram_message") and hasattr(_tg_api, "send_telegram"):
        def send_telegram_message(text: str) -> None:
            _tg_api.send_telegram(text)
        _tg_api.send_telegram_message = send_telegram_message  # patch-in
        log.info("Patched telegram.api.send_telegram_message -> send_telegram")

# 2) utils.http: provide get_json wrapper expected by some core modules
try:
    import utils.http as _http
    if not hasattr(_http, "get_json"):
        def get_json(url: str, params: Optional[dict] = None,
                     timeout: int = 10, retries: int = 1):
            resp = _http.safe_get(url, params=params or {}, timeout=timeout, retries=retries)
            return _http.safe_json(resp)
        _http.get_json = get_json  # patch-in
        log.info("Patched utils.http.get_json using safe_get + safe_json")
except Exception as e:
    log.error("Failed to import/patch utils.http: %s", e)

# Re-export a simple sender for convenience
def send(text: str) -> None:
    try:
        if _tg_api and hasattr(_tg_api, "send_telegram"):
            _tg_api.send_telegram(text)
        else:
            log.info("[TELEGRAM DISABLED] %s", text)
    except Exception as e:
        log.warning("Telegram send failed: %s", e)

# --- Telegram long-poll ----------------------------------------------------
import requests

def _tg_api_get(method: str, **params):
    if not TELEGRAM_BOT_TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.get(url, params=params, timeout=50)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug("tg api get error %s: %s", method, e)
    return None

def telegram_long_poll_loop():
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; Telegram poller disabled.")
        return
    try:
        from telegram.dispatcher import dispatch
    except Exception as e:
        log.error("Failed to import telegram.dispatcher: %s", e)
        return

    send("🤖 Telegram command handler online.")
    offset = None
    while not _shutdown.is_set():
        try:
            resp = _tg_api_get("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
            if not resp or not resp.get("ok"):
                time.sleep(2)
                continue
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                # Let dispatcher parse & route the command
                try:
                    reply = dispatch(text)
                    if reply:
                        send(reply)
                except Exception as de:
                    log.exception("dispatch error: %s", de)
        except Exception as e:
            log.debug("telegram poll error: %s", e)
            time.sleep(2)

# --- Reports scheduler (daily & intraday) ----------------------------------
def scheduler_loop():
    try:
        from reports.scheduler import start_eod_scheduler, run_pending
    except Exception as e:
        log.error("Failed to import reports.scheduler: %s", e)
        return

    # Bridge reports.scheduler → telegram.api (expects send_telegram_message)
    if _tg_api and not hasattr(_tg_api, "send_telegram_message") and hasattr(_tg_api, "send_telegram"):
        _tg_api.send_telegram_message = _tg_api.send_telegram

    eod_str = start_eod_scheduler()
    send(f"⏱ Scheduler online (EOD {eod_str}).")
    while not _shutdown.is_set():
        try:
            run_pending()
        except Exception as e:
            log.debug("scheduler run_pending error: %s", e)
        # light sleep to reduce CPU
        for _ in range(6):
            if _shutdown.is_set():
                break
            time.sleep(5)

# --- (Optional) future: core monitors --------------------------------------
# We can enable wallet/discovery monitors once minor import fixes in core/*.py land.
# def start_monitors():
#     from core.wallet_monitor import WalletMonitor
#     ...

# --- Graceful shutdown ------------------------------------------------------
_shutdown = threading.Event()

def _graceful_exit(signum, frame):
    try:
        send("🛑 Shutting down.")
    except Exception:
        pass
    _shutdown.set()

# --- Main -------------------------------------------------------------------
def main():
    send("🟢 Starting Cronos DeFi Sentinel (compat-mode).")

    # Start Telegram & Scheduler threads
    threads = [
        threading.Thread(target=telegram_long_poll_loop, name="telegram", daemon=True),
        threading.Thread(target=scheduler_loop, name="scheduler", daemon=True),
    ]
    for t in threads:
        t.start()

    # Keep alive
    while not _shutdown.is_set():
        time.sleep(1)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log.exception("fatal: %s", e)
        try:
            send(f"💥 Fatal error: {e}")
        except Exception:
            pass
        sys.exit(1)
