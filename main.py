#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (updated, full)
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
"""

import os, sys, time, json, threading, logging, signal, random
from collections import deque, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

getcontext().prec = 28

from utils.http import safe_get, safe_json
from telegram.api import send_telegram, escape_md
from reports.day_report import build_day_report_text as _compose_day_report
from reports.ledger import append_ledger, update_cost_basis as ledger_update_cost_basis, replay_cost_basis_over_entries
from reports.aggregates import aggregate_per_asset

load_dotenv()

def _alias_env(src, dst):
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)
_alias_env("ALERTS_INTERVAL_MINUTES", "ALERTS_INTERVAL_MIN")
_alias_env("DISCOVER_REQUIRE_WCRO_QUOTE", "DISCOVER_REQUIRE_WCRO")

def _init_tz(tz_str: str | None):
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
def now_dt(): return datetime.now(LOCAL_TZ)
def ymd(dt=None): return (dt or now_dt()).strftime("%Y-%m-%d")
def month_prefix(dt=None): return (dt or now_dt()).strftime("%Y-%m")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API") or ""
CRONOS_RPC_URL     = os.getenv("CRONOS_RPC_URL") or ""

TOKENS             = os.getenv("TOKENS", "")
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")

LOG_SCAN_BLOCKS = int(os.getenv("LOG_SCAN_BLOCKS", "120000"))
LOG_SCAN_CHUNK  = int(os.getenv("LOG_SCAN_CHUNK",  "5000"))
WALLET_POLL     = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL        = int(os.getenv("DEX_POLL", "60"))
PRICE_WINDOW    = int(os.getenv("PRICE_WINDOW","3"))
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

RECEIPT_SYMBOLS = set([s.strip().upper() for s in os.getenv("RECEIPT_SYMBOLS","TCRO").split(",") if s.strip()])

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"
DATA_DIR         = "/app/data"
ATH_PATH         = os.path.join(DATA_DIR, "ath.json")
OFFSET_PATH      = os.path.join(DATA_DIR, "telegram_offset.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("wallet-monitor")

# (συνέχεια στο Part 2/2)
# ... συνέχεια από το Part 1 ...

# ---------- Holdings formatter (inline) ----------
def _fmt_holdings_text():
    total, breakdown, unrealized, receipts = compute_holdings_merged()
    lines=["*📦 Holdings (merged):*"]
    if breakdown:
        for b in breakdown:
            lines.append(f"• {escape_md(b['token'])}: {_format_amount(b['amount'])}  @ ${_format_price(b.get('price_usd',0))}  = ${_format_amount(b.get('usd_value',0))}")
    else:
        lines.append("• No holdings (RPC snapshot or history positions empty).")
    if receipts:
        lines.append("\n*Receipts:*")
        for r in receipts:
            lines.append(f"• {escape_md(r['token'])}: {_format_amount(r['amount'])}")
    lines.append(f"\nΣύνολο: ${_format_amount(total)}")
    if _nonzero(unrealized):
        lines.append(f"Unrealized: ${_format_amount(unrealized)}")
    return "\n".join(lines)

# ---------- Command handler ----------
def _handle_command(text: str):
    t = text.strip()
    low = t.lower()

    if low.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
    elif low.startswith("/diag"):
        send_telegram(
            "🔧 Diagnostics\n"
            f"WALLETADDRESS: {escape_md(WALLET_ADDRESS)}\n"
            f"CRONOSRPCURL set: {bool(CRONOS_RPC_URL)}\n"
            f"Etherscan key: {bool(ETHERSCAN_API)}\n"
            f"LOGSCANBLOCKS={LOG_SCAN_BLOCKS} LOGSCANCHUNK={LOG_SCAN_CHUNK}\n"
            f"TZ={escape_md(TZ)} INTRADAYHOURS={INTRADAY_HOURS} EOD={EOD_HOUR:02d}:{EOD_MINUTE:02d}\n"
            f"Alerts every: {ALERTS_INTERVAL_MIN}m | Pump/Dump: {PUMP_ALERT_24H_PCT}/{DUMP_ALERT_24H_PCT}\n"
            f"Tracked pairs: {escape_md(', '.join(sorted(_tracked_pairs)) or '(none)')}"
        )
    elif low.startswith("/rescan"):
        cnt = rpc_discover_wallet_tokens()
        send_telegram(f"🔄 Rescan done. Positive tokens: {cnt}")
    elif low in ["/holdings", "/show_wallet_assets", "/showwalletassets", "/showassets", "/show"]:
        try:
            send_telegram(_fmt_holdings_text())
        except Exception as e:
            send_telegram(f"❌ Error fetching holdings:\n`{e}`")
    elif low.startswith("/dailysum") or low.startswith("/showdaily"):
        send_telegram(_format_daily_sum_message())
    elif low.startswith("/report"):
        send_telegram(build_day_report_text())
    elif low.startswith("/totals"):
        parts = low.split()
        scope = parts[1] if len(parts) > 1 and parts[1] in ("today", "month", "all") else "all"
        send_telegram(format_totals(scope))
    elif low.startswith("/totalstoday"):
        send_telegram(format_totals("today"))
    elif low.startswith("/totalsmonth"):
        send_telegram(format_totals("month"))
    elif low.startswith("/pnl"):
        parts = low.split()
        scope = parts[1] if len(parts) > 1 and parts[1] in ("today", "month", "all") else "all"
        send_telegram(format_totals(scope))
    elif low.startswith("/watch "):
        try:
            _, rest = low.split(" ",1)
            if rest.startswith("add "):
                pair = rest.split(" ",1)[1].strip().lower()
                if pair.startswith("cronos/"):
                    ensure_tracking_pair("cronos", pair.split("/",1)[1])
                    send_telegram(f"👁 Added {escape_md(pair)}")
                else:
                    send_telegram("Use format cronos/<pairAddress>")
            elif rest.startswith("rm "):
                pair = rest.split(" ",1)[1].strip().lower()
                if pair in _tracked_pairs:
                    _tracked_pairs.remove(pair)
                    send_telegram(f"🗑 Removed {escape_md(pair)}")
                else:
                    send_telegram("Pair not tracked.")
            elif rest.strip() == "list":
                send_telegram("👁 Tracked:\n"+escape_md("\n".join(sorted(_tracked_pairs))) if _tracked_pairs else "None.")
            else:
                send_telegram("Usage: /watch add <cronos/pair> | /watch rm <cronos/pair> | /watch list")
        except Exception as e:
            send_telegram(f"Watch error: {escape_md(str(e))}")
    else:
        send_telegram("❓ Commands: /status /diag /rescan /holdings /show /dailysum /report /totals [today|month|all] /totalstoday /totalsmonth /pnl [scope] /watch ...")

# ---------- Main ----------
def _graceful_exit(signum, frame):
    try: send_telegram("🛑 Shutting down.")
    except: pass
    shutdown_event.set()

def main():
    load_ath()
    send_telegram("🟢 Starting Cronos DeFi Sentinel.")

    threads = [
        threading.Thread(target=discovery_loop, name="discovery"),
        threading.Thread(target=wallet_monitor_loop, name="wallet"),
        threading.Thread(target=monitor_tracked_pairs_loop, name="dex"),
        threading.Thread(target=alerts_monitor_loop, name="alerts"),
        threading.Thread(target=guard_monitor_loop, name="guard"),
        threading.Thread(target=telegram_long_poll_loop, name="telegram"),
        threading.Thread(target=_scheduler_loop, name="scheduler"),
    ]
    for t in threads: t.start()
    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    finally:
        for t in threads:
            t.join(timeout=5)

if __name__=="__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log.exception("fatal: %s", e)
        try: send_telegram(f"💥 Fatal error: {escape_md(str(e))}")
        except: pass
        sys.exit(1)
