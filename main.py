#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main orchestrator (integrated)
- Env load & validation
- Telegram boot ping + command dispatcher
- PriceWatcher loop (Dexscreener-backed pricing via core.pricing)
- WalletMonitor loop (real-time tx feed → ledger + alerts)
- Schedulers: EOD + optional Intraday
- Guards scope refresh from live holdings
- Graceful shutdown
"""
from __future__ import annotations
import os
import sys
import time
import logging
import signal
from decimal import getcontext
from typing import Optional

from dotenv import load_dotenv

getcontext().prec = 28

from core.config import apply_env_aliases, validate_env
from core.watch import make_from_env
from core.wallet_monitor import make_wallet_monitor
from core.providers.cronos import fetch_wallet_txs
from reports.scheduler import start_eod_scheduler, run_pending
from telegram.api import send_telegram_message
from telegram.dispatcher import dispatch

_state_lock = None
_shutdown = False
_updates_offset: Optional[int] = None
_watcher = None
_wallet_mon = None


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO), format="%(asctime)s %(levelname)s %(message)s")


def _handle_sigterm(signum, frame):  # noqa: ARG001
    global _shutdown
    logging.info("Signal received: %s — shutting down gracefully…", signum)
    _shutdown = True


def _poll_telegram_once() -> None:
    global _updates_offset
    try:
        import telegram.api as tg
        get_updates = getattr(tg, "get_updates", None)
        if not callable(get_updates):
            return
        resp = get_updates(offset=_updates_offset, timeout=15) or {}
        if not bool(resp.get("ok", True)):
            logging.warning("telegram.get_updates not ok: %s", resp)
            return
        for upd in resp.get("result", []):
            _updates_offset = max(_updates_offset or 0, upd.get("update_id", 0) + 1)
            msg = upd.get("message") or upd.get("edited_message") or {}
            if not msg:
                continue
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text")
            if not text:
                continue
            try:
                reply = dispatch(text, chat_id)
                if reply:
                    send_telegram_message(reply)
            except Exception as e:  # noqa: BLE001
                logging.exception("dispatch failed: %s", e)
    except Exception as e:  # noqa: BLE001
        logging.debug("telegram inbound poll skipped/failed: %s", e)


def boot_init() -> None:
    global _state_lock, _watcher, _wallet_mon
    load_dotenv()
    _setup_logging()
    apply_env_aliases()

    try:
        ok, _ = validate_env(strict=False)
        if not ok:
            logging.warning("[env] Environment incomplete; check warnings above.")
    except Exception as e:  # noqa: BLE001
        logging.warning("[env] Validation error: %s", e)

    try:
        send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")
    except Exception as e:  # noqa: BLE001
        logging.warning("Telegram boot ping failed: %s", e)

    try:
        at = start_eod_scheduler()
        logging.info("EOD scheduler armed at %s local time", at)
    except Exception as e:  # noqa: BLE001
        logging.warning("EOD scheduler init failed: %s", e)

    _watcher = make_from_env()
    _wallet_mon = make_wallet_monitor(provider=fetch_wallet_txs)

    try:
        import threading
        _state_lock = threading.Lock()
    except Exception:  # pragma: no cover
        pass
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)


def main() -> int:
    boot_init()
    interval = int(os.getenv("WALLET_POLL", "15") or 15)
    _holdings_refresh_sec = int(os.getenv("HOLDINGS_REFRESH_SEC", "60") or 60)
    _last_holdings = 0.0

    while not _shutdown:
        loop_start = time.time()
        try:
            if _state_lock:
                with _state_lock:
                    _watcher.poll_once()
                    if _wallet_mon:
                        _wallet_mon.poll_once()
            else:
                _watcher.poll_once()
                if _wallet_mon:
                    _wallet_mon.poll_once()

            # schedulers
            run_pending()

            # refresh guards scope from holdings periodically
            try:
                if time.time() - _last_holdings >= _holdings_refresh_sec:
                    from core.holdings import get_wallet_snapshot
                    from core.guards import set_holdings
                    w = os.getenv("WALLET_ADDRESS", "")
                    snap = get_wallet_snapshot(w) or {}
                    set_holdings(set(snap.keys()))
                    _last_holdings = time.time()
            except Exception:
                pass

            # telegram inbound
            _poll_telegram_once()

        except Exception as e:  # noqa: BLE001
            logging.exception("main loop error: %s", e)

        elapsed = time.time() - loop_start
        sleep_for = max(0.5, interval - elapsed)
        t0 = time.time()
        while not _shutdown and (time.time() - t0) < sleep_for:
            time.sleep(0.5)

    logging.info("Shutdown complete.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        logging.exception("fatal: %s", e)
        try:
            send_telegram_message(f"💥 Fatal error: {e}")
        except Exception:
            pass
        sys.exit(1)
