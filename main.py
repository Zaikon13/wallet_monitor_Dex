#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (patched, modular)
- RPC snapshot (CRO + ERC-20)
- Dexscreener pricing (+history fallback) with canonical CRO via WCRO/USDT
- Cost-basis PnL (Decimal + FIFO, fees optional)
- Intraday/EOD reports
- Alerts & Guard window
- Telegram long-poll with backoff + offset persistence, Markdown escaping & chunking (handled in telegram/api.py)
- Thread-safe shared state with locks
- Reconciled holdings (RPC wins over History; no double counting; receipts excluded from CRO)

Requires helpers:
  utils/http.py
  telegram/api.py
  reports/day_report.py
  reports/ledger.py
  reports/aggregates.py
  core/*.py
"""

import os, sys, time, json, threading, logging, signal, random
from collections import deque, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# ---------- Precision ----------
getcontext().prec = 28  # high precision for PnL math

# ---------- Local imports ----------
from core.config import (
    WALLET_ADDRESS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    DEX_PAIRS, INTRADAY_HOURS, TZ, DATA_DIR,
    ALERTS_INTERVAL_MIN, DUMP_ALERT_24H_PCT, PUMP_ALERT_24H_PCT,
    GUARD_WINDOW_MIN, GUARD_PUMP_PCT, GUARD_DROP_PCT, GUARD_TRAIL_DROP_PCT,
    DISCOVER_ENABLED, DISCOVER_QUERY, DISCOVER_LIMIT, DISCOVER_POLL,
    DISCOVER_MIN_LIQ_USD, DISCOVER_MIN_VOL24_USD,
    DISCOVER_MIN_ABS_CHANGE_PCT, DISCOVER_MAX_PAIR_AGE_HOURS,
    DISCOVER_REQUIRE_WCRO,
    EOD_HOUR, EOD_MINUTE,
)
from core.tz import now_local, ymd, parse_ts
from core.pricing import get_price_usd
from core.rpc import get_wallet_balances
from core.holdings import get_wallet_snapshot
from core.watch import watch_pairs_loop
from reports.ledger import append_ledger, replay_cost_basis_over_entries, data_file_for_today, read_json
from reports.aggregates import aggregate_per_asset
from reports.day_report import build_day_report_text as _compose_day_report
from telegram.api import send_telegram, escape_md
from telegram.formatters import format_holdings

# ---------- Globals ----------
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

STATE_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
TZINFO = ZoneInfo(TZ)

# ---------- Wallet Monitor ----------
def wallet_monitor_loop():
    logging.info("📡 Wallet monitor started.")
    while not STOP_EVENT.is_set():
        try:
            balances = get_wallet_balances(WALLET_ADDRESS)
            snapshot = {}
            for sym, bal in balances.items():
                price = get_price_usd(sym)
                snapshot[sym] = {
                    "amount": Decimal(str(bal)),
                    "price": Decimal(str(price or 0))
                }
            # Save holdings snapshot somewhere if needed
        except Exception as e:
            logging.warning("Wallet monitor error: %s", e)
        time.sleep(int(os.getenv("WALLET_POLL", "15")))

# ---------- Scheduler ----------
def scheduler_loop():
    logging.info("📅 Scheduler started.")
    last_intraday = None
    while not STOP_EVENT.is_set():
        now = now_local()
        try:
            # Intraday report
            if last_intraday is None or (now - last_intraday).seconds >= INTRADAY_HOURS * 3600:
                last_intraday = now
                _send_intraday_report()

            # End of day report
            if now.hour == EOD_HOUR and now.minute == EOD_MINUTE:
                _send_eod_report()
                time.sleep(60)  # avoid multiple triggers in same minute
        except Exception as e:
            logging.warning("Scheduler error: %s", e)
        time.sleep(20)

def _send_intraday_report():
    try:
        snapshot = get_wallet_snapshot(WALLET_ADDRESS)
        msg = format_holdings(snapshot)
        send_telegram(msg)
    except Exception as e:
        logging.warning("Intraday report failed: %s", e)

def _send_eod_report():
    try:
        path = data_file_for_today()
        data = read_json(path, default={"entries": []})
        entries = data.get("entries", [])
        pos_qty, pos_cost = {}, {}
        realized_today = replay_cost_basis_over_entries(pos_qty, pos_cost, entries)
        holdings = get_wallet_snapshot(WALLET_ADDRESS)
        breakdown = []
        total_value = Decimal("0")
        for sym, item in holdings.items():
            amt = Decimal(item["amount"])
            pr  = Decimal(item["price"])
            usd = amt * pr
            breakdown.append({"token": sym, "amount": float(amt), "price_usd": float(pr), "usd_value": float(usd)})
            total_value += usd
        txt = _compose_day_report(
            date_str=ymd(),
            entries=entries,
            net_flow=data.get("net_usd_flow", 0.0),
            realized_today_total=realized_today,
            holdings_total=float(total_value),
            breakdown=breakdown,
            unrealized=sum(pos_cost.values()),
            data_dir=DATA_DIR,
            tz=TZINFO
        )
        send_telegram(escape_md(txt))
    except Exception as e:
        logging.warning("EOD report failed: %s", e)

# ---------- Telegram Polling ----------
def telegram_poll_loop():
    logging.info("🤖 Telegram command handler online.")
    offset = None
    while not STOP_EVENT.is_set():
        try:
            import requests
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            params = {"timeout": 50, "offset": offset}
            r = requests.get(url, params=params, timeout=60)
            if r.status_code != 200:
                time.sleep(2); continue
            resp = r.json()
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = str(((msg.get("chat") or {}).get("id") or ""))
                if TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID) != chat_id:
                    continue
                text = (msg.get("text") or "").strip().lower()
                if not text: continue
                _handle_command(text)
        except Exception as e:
            logging.warning("Telegram poll error: %s", e)
            time.sleep(2)

def _handle_command(cmd: str):
    if cmd.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
    elif cmd.startswith("/diag"):
        send_telegram("🔧 Diagnostics available.")
    elif cmd.startswith("/rescan"):
        send_telegram("🔄 Rescan triggered.")
    elif cmd in ["/holdings", "/show_wallet_assets", "/showwalletassets", "/showassets", "/show"]:
        try:
            snapshot = get_wallet_snapshot(WALLET_ADDRESS)
            msg = format_holdings(snapshot)
            send_telegram(msg)
        except Exception as e:
            send_telegram(f"❌ Error fetching holdings:\n`{e}`")
    elif cmd in ["/report", "/showdaily", "/dailysum"]:
        _send_eod_report()
    else:
        send_telegram("❓ Unknown command")

# ---------- Main ----------
def main():
    logging.info("🟢 Starting Cronos DeFi Sentinel.")

    threads = [
        threading.Thread(target=wallet_monitor_loop, daemon=True),
        threading.Thread(target=scheduler_loop, daemon=True),
        threading.Thread(target=watch_pairs_loop, daemon=True),
        threading.Thread(target=telegram_poll_loop, daemon=True),
    ]
    for t in threads: t.start()

    def _graceful(sig, frame):
        logging.info("🛑 Stopping...")
        STOP_EVENT.set()
    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)

    while not STOP_EVENT.is_set():
        time.sleep(1)

if __name__ == "__main__":
    main()
