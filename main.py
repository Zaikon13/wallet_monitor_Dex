#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel - Wallet Monitor (Cronos chain) + Dexscreener + Web3 RPC
"""

import os
import sys
import time
import json
import signal
import threading
import logging
from collections import deque, defaultdict
from datetime import datetime, timedelta
import math
import requests
from dotenv import load_dotenv

# externalized helpers
from utils.http import safe_get, safe_json
from telegram.api import send_telegram
from reports.ledger import read_json, write_json, data_file_for_today, append_ledger

try:
    from reports.ledger import (
        update_cost_basis as ledger_update_cost_basis,
        replay_cost_basis_over_entries as ledger_replay_cost_basis_over_entries,
    )
except Exception:
    ledger_update_cost_basis = None
    ledger_replay_cost_basis_over_entries = None

# ------------------------------------------------------------
# Bootstrap
# ------------------------------------------------------------
load_dotenv()

def _alias_env(src: str, dst: str):
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)

_alias_env("ALERTS_INTERVAL_MINUTES", "ALERTS_INTERVAL_MIN")
_alias_env("DISCOVER_REQUIRE_WCRO_QUOTE", "DISCOVER_REQUIRE_WCRO")

import time as _time
from zoneinfo import ZoneInfo

def _init_tz(tz_str: str | None):
    tz = tz_str or "Europe/Athens"
    os.environ["TZ"] = tz
    try:
        if hasattr(_time, "tzset"):
            _time.tzset()
    except Exception:
        pass
    return ZoneInfo(tz)

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = _init_tz(TZ)

def now_dt():
    return datetime.now(LOCAL_TZ)

def ymd(dt=None):
    if dt is None:
        dt = now_dt()
    return dt.strftime("%Y-%m-%d")

def month_prefix(dt=None):
    if dt is None:
        dt = now_dt()
    return dt.strftime("%Y-%m")

# ------------------------------------------------------------
# Config / ENV
# ------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API") or ""

CRONOS_RPC_URL  = os.getenv("CRONOS_RPC_URL") or ""
LOG_SCAN_BLOCKS = int(os.getenv("LOG_SCAN_BLOCKS", "120000"))
LOG_SCAN_CHUNK  = int(os.getenv("LOG_SCAN_CHUNK",  "5000"))

TOKENS    = os.getenv("TOKENS", "")
DEX_PAIRS = os.getenv("DEX_PAIRS", "")

WALLET_POLL = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL    = int(os.getenv("DEX_POLL", "60"))
PRICE_WINDOW= int(os.getenv("PRICE_WINDOW","3"))
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD","5"))
SPIKE_THRESHOLD      = float(os.getenv("SPIKE_THRESHOLD","8"))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT","0"))

DISCOVER_ENABLED  = (os.getenv("DISCOVER_ENABLED","true").lower() in ("1","true","yes","on"))
DISCOVER_QUERY    = os.getenv("DISCOVER_QUERY","cronos")
DISCOVER_LIMIT    = int(os.getenv("DISCOVER_LIMIT","10"))
DISCOVER_POLL     = int(os.getenv("DISCOVER_POLL","120"))
DISCOVER_MIN_LIQ_USD        = float(os.getenv("DISCOVER_MIN_LIQ_USD","30000"))
DISCOVER_MIN_VOL24_USD      = float(os.getenv("DISCOVER_MIN_VOL24_USD","5000"))
DISCOVER_MIN_ABS_CHANGE_PCT = float(os.getenv("DISCOVER_MIN_ABS_CHANGE_PCT","10"))
DISCOVER_MAX_PAIR_AGE_HOURS = int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS","24"))
DISCOVER_REQUIRE_WCRO       = (os.getenv("DISCOVER_REQUIRE_WCRO","false").lower() in ("1","true","yes","on"))
DISCOVER_BASE_WHITELIST     = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_WHITELIST","").split(",") if s.strip()]
DISCOVER_BASE_BLACKLIST     = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_BLACKLIST","").split(",") if s.strip()]

INTRADAY_HOURS  = int(os.getenv("INTRADAY_HOURS","3"))
EOD_HOUR        = int(os.getenv("EOD_HOUR","23"))
EOD_MINUTE      = int(os.getenv("EOD_MINUTE","59"))

ALERTS_INTERVAL_MIN = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
DUMP_ALERT_24H_PCT  = float(os.getenv("DUMP_ALERT_24H_PCT","-15"))
PUMP_ALERT_24H_PCT  = float(os.getenv("PUMP_ALERT_24H_PCT","20"))

GUARD_WINDOW_MIN     = int(os.getenv("GUARD_WINDOW_MIN","60"))
GUARD_PUMP_PCT       = float(os.getenv("GUARD_PUMP_PCT","20"))
GUARD_DROP_PCT       = float(os.getenv("GUARD_DROP_PCT","-12"))
GUARD_TRAIL_DROP_PCT = float(os.getenv("GUARD_TRAIL_DROP_PCT","-8"))

# ------------------------------------------------------------
# Logging
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wallet-monitor")

# ------------------------------------------------------------
# Runtime state
# ------------------------------------------------------------
shutdown_event = threading.Event()
_seen_tx_hashes   = set()
_last_prices      = {}
_price_history    = {}
_last_pair_tx     = {}
_tracked_pairs    = set()
_known_pairs_meta = {}

_token_balances = defaultdict(float)
_token_meta     = {}

_position_qty   = defaultdict(float)
_position_cost  = defaultdict(float)
_realized_pnl_today = 0.0

EPSILON = 1e-12
_last_intraday_sent = 0.0

ATH = {}
_alert_last_sent = {}
COOLDOWN_SEC = 60 * 30
_guard = {}

DATA_DIR = "/app/data"
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

# ------------------------------------------------------------
# Cost-basis helpers (fallbacks)
# ------------------------------------------------------------
def _fallback_update_cost_basis(pos_qty, pos_cost, token_key, signed_amount, price_usd, eps=1e-12):
    qty = float(pos_qty.get(token_key, 0.0))
    cost = float(pos_cost.get(token_key, 0.0))
    realized = 0.0
    if signed_amount > eps:
        buy_qty = signed_amount
        pos_qty[token_key] = qty + buy_qty
        pos_cost[token_key] = cost + buy_qty * (price_usd or 0.0)
    elif signed_amount < -eps:
        sell_qty_req = -signed_amount
        if qty > eps:
            sell_qty = min(sell_qty_req, qty)
            avg_cost = (cost / qty) if qty > eps else (price_usd or 0.0)
            realized = (price_usd - avg_cost) * sell_qty
            pos_qty[token_key] = qty - sell_qty
            pos_cost[token_key] = max(0.0, cost - avg_cost * sell_qty)
    return realized

def _use_update_cost_basis():
    return ledger_update_cost_basis or _fallback_update_cost_basis

def _fallback_replay_cost_basis_over_entries(pos_qty, pos_cost, entries, eps=1e-12):
    pos_qty.clear()
    pos_cost.clear()
    updater = _use_update_cost_basis()
    for e in entries or []:
        token_addr = (e.get("token_addr") or "").strip().lower()
        sym = (e.get("token") or "").strip()
        key = token_addr if (token_addr.startswith("0x") and len(token_addr) == 42) else ("CRO" if sym.upper() in ("CRO","TCRO") else sym)
        amt = float(e.get("amount") or 0.0)
        pr  = float(e.get("price_usd") or 0.0)
        updater(pos_qty, pos_cost, key, amt, pr, eps=eps)

def _use_replay_cost_basis_over_entries():
    return ledger_replay_cost_basis_over_entries or _fallback_replay_cost_basis_over_entries
# ------------------------------------------------------------
# Ledger replay hook (στο wallet monitor)
# ------------------------------------------------------------
def replay_today_cost_basis():
    path = data_file_for_today()
    data = read_json(path, default={"entries": []})
    entries = data.get("entries", [])
    _use_replay_cost_basis_over_entries()(_position_qty, _position_cost, entries)


# ------------------------------------------------------------
# Wallet monitor loop & schedulers
# ------------------------------------------------------------
def wallet_monitor_loop():
    log.info("Wallet monitor starting; loading initial recent txs...")
    initial = fetch_latest_wallet_txs(limit=50)
    try:
        for tx in initial:
            h = tx.get("hash")
            if h:
                _seen_tx_hashes.add(h)
    except Exception:
        pass

    replay_today_cost_basis()
    if WALLET_ADDRESS:
        send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}` (Cronos).")

    last_tokentx_seen = set()
    while not shutdown_event.is_set():
        try:
            txs = fetch_latest_wallet_txs(limit=25)
            for tx in reversed(txs):
                if not isinstance(tx, dict):
                    continue
                h = tx.get("hash")
                if h in _seen_tx_hashes:
                    continue
                handle_native_tx(tx)
        except Exception as e:
            log.exception("wallet native loop error: %s", e)

        try:
            toks = fetch_latest_token_txs(limit=60)
            for t in reversed(toks):
                h = t.get("hash")
                if h and h in last_tokentx_seen:
                    continue
                handle_erc20_tx(t)
                if h:
                    last_tokentx_seen.add(h)
            if len(last_tokentx_seen) > 600:
                last_tokentx_seen = set(list(last_tokentx_seen)[-400:])
        except Exception as e:
            log.exception("wallet token loop error: %s", e)

        for _ in range(WALLET_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)


def run_with_restart(fn, name, daemon=True):
    def runner():
        while not shutdown_event.is_set():
            try:
                log.info("Thread %s starting.", name)
                fn()
                log.info("Thread %s exited cleanly.", name)
                break
            except Exception as e:
                log.exception("Thread %s crashed: %s. Restarting in 3s...", name, e)
                for _ in range(3):
                    if shutdown_event.is_set():
                        break
                    time.sleep(1)
        log.info("Thread %s terminating.", name)
    t = threading.Thread(target=runner, daemon=daemon, name=name)
    t.start()
    return t


# ------------------------------------------------------------
# Entrypoint
# ------------------------------------------------------------
def main():
    log.info("Starting monitor with config:")
    log.info("WALLET_ADDRESS: %s", WALLET_ADDRESS)
    log.info("TELEGRAM_BOT_TOKEN present: %s", bool(TELEGRAM_BOT_TOKEN))
    log.info("TELEGRAM_CHAT_ID: %s", TELEGRAM_CHAT_ID)
    log.info("ETHERSCAN_API present: %s", bool(ETHERSCAN_API))
    log.info("CRONOS_RPC_URL set: %s", bool(CRONOS_RPC_URL))
    log.info("LOG_SCAN_BLOCKS=%s LOG_SCAN_CHUNK=%s", LOG_SCAN_BLOCKS, LOG_SCAN_CHUNK)
    log.info("DEX_PAIRS: %s", DEX_PAIRS)
    log.info("DISCOVER_ENABLED: %s | DISCOVER_QUERY: %s", DISCOVER_ENABLED, DISCOVER_QUERY)
    log.info("TZ: %s | INTRADAY_HOURS: %s | EOD: %02d:%02d", TZ, INTRADAY_HOURS, EOD_HOUR, EOD_MINUTE)
    log.info("Alerts interval: %sm | Wallet 24h dump/pump: %s/%s", ALERTS_INTERVAL_MIN, PUMP_ALERT_24H_PCT, DUMP_ALERT_24H_PCT)

    try:
        _ = replay_today_cost_basis()
        log.info("Ledger replay initialized.")
    except Exception as e:
        log.warning("Ledger replay init failed: %s", e)

    threads = []
    threads.append(run_with_restart(wallet_monitor_loop, "wallet_monitor"))
    threads.append(run_with_restart(monitor_tracked_pairs_loop, "pairs_monitor"))
    threads.append(run_with_restart(discovery_loop, "discovery"))
    threads.append(run_with_restart(intraday_report_loop, "intraday_report"))
    threads.append(run_with_restart(end_of_day_scheduler_loop, "eod_scheduler"))
    threads.append(run_with_restart(alerts_monitor_loop, "alerts_monitor"))
    threads.append(run_with_restart(guard_monitor_loop, "guard_monitor"))
    threads.append(run_with_restart(telegram_commands_loop, "telegram_commands"))

    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt: shutting down...")
        shutdown_event.set()

    log.info("Waiting for threads to terminate...")
    for t in threading.enumerate():
        if t is threading.current_thread():
            continue
        t.join(timeout=2)
    log.info("Shutdown complete.")


def _signal_handler(sig, frame):
    log.info("Signal %s received, initiating shutdown...", sig)
    shutdown_event.set()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

if __name__ == "__main__":
    main()
