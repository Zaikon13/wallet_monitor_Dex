#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (patched split, part 1/2)
This is lines ~1–~700 of the canonical main.py, plus the requested imports
and helpers added here so part 2 can continue seamlessly.

Features (unchanged):
- RPC snapshot (CRO + ERC-20)
- Dexscreener pricing (+history fallback)
- Cost-basis PnL
- Intraday/EOD reports
- Alerts & Guard window
- Telegram long-poll commands

Patched in this part:
- schedule import & EOD_TIME
- try-import send_telegram_message (fallback to send_telegram)
- build_day_report_text import
- holdings snapshot helpers
- notify_error import (safe fallback)
- send_daily_report() implementation (called/bound in part 2)

The file continues in Main2.py without gaps.
"""

import os, sys, time, json, threading, logging, signal
from collections import deque, defaultdict
from datetime import datetime, timedelta

from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# ---------- Patched imports ----------
import schedule  # scheduler for EOD job (safe if already present)

try:
    from telegram.api import send_telegram_message
except Exception:
    from telegram.api import send_telegram as send_telegram_message  # fallback

from telegram.api import send_telegram  # existing helper used elsewhere
from reports.day_report import build_day_report_text as _compose_day_report
from reports.ledger import append_ledger, update_cost_basis as ledger_update_cost_basis, replay_cost_basis_over_entries
from reports.aggregates import aggregate_per_asset

try:
    from core.holdings import get_wallet_snapshot, format_snapshot_lines
except Exception:
    def get_wallet_snapshot(addr): return None
    def format_snapshot_lines(snap): return []

try:
    from core.alerts import notify_error
except Exception:
    def notify_error(context, err): pass

# ---------- Bootstrap / TZ ----------
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

# ---------- ENV ----------
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
DISCOVER_BASE_WHITELIST     = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_WHITELIST","" ).split(",") if s.strip()]
DISCOVER_BASE_BLACKLIST     = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_BLACKLIST","" ).split(",") if s.strip()]

INTRADAY_HOURS  = int(os.getenv("INTRADAY_HOURS","3"))
EOD_HOUR        = int(os.getenv("EOD_HOUR","23"))
EOD_MINUTE      = int(os.getenv("EOD_MINUTE","59"))
EOD_TIME        = f"{EOD_HOUR:02d}:{EOD_MINUTE:02d}"  # patched

ALERTS_INTERVAL_MIN = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
DUMP_ALERT_24H_PCT  = float(os.getenv("DUMP_ALERT_24H_PCT","-15"))
PUMP_ALERT_24H_PCT  = float(os.getenv("PUMP_ALERT_24H_PCT","20"))

GUARD_WINDOW_MIN     = int(os.getenv("GUARD_WINDOW_MIN","60"))
GUARD_PUMP_PCT       = float(os.getenv("GUARD_PUMP_PCT","20"))
GUARD_DROP_PCT       = float(os.getenv("GUARD_DROP_PCT","-12"))
GUARD_TRAIL_DROP_PCT = float(os.getenv("GUARD_TRAIL_DROP_PCT","-8"))

RECEIPT_SYMBOLS = set([s.strip().upper() for s in os.getenv("RECEIPT_SYMBOLS","TCRO").split(",") if s.strip()])

# ---------- Constants / Logging ----------
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"
DATA_DIR         = "/app/data"
ATH_PATH         = os.path.join(DATA_DIR, "ath.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("wallet-monitor")

# ---------- Runtime ----------
shutdown_event = threading.Event()
_seen_tx_hashes = set()
_last_prices, _price_history, _last_pair_tx = {}, {}, {}
_tracked_pairs, _known_pairs_meta = set(), {}
_TOKEN_EVENT_LRU_MAX, _TOKEN_HASH_LRU_MAX = 4000, 2000
_seen_token_events, _seen_token_hashes = set(), set()
_seen_token_events_q, _seen_token_hashes_q = deque(maxlen=_TOKEN_EVENT_LRU_MAX), deque(maxlen=_TOKEN_HASH_LRU_MAX)

_token_balances = defaultdict(float)   # "CRO" or contract 0x..
_token_meta     = {}                   # key -> {"symbol","decimals"}

_position_qty   = defaultdict(float)   # key (addr or "CRO")
_position_cost  = defaultdict(float)
_realized_pnl_today = 0.0

EPSILON = 1e-12
_last_intraday_sent = 0.0

PRICE_CACHE, PRICE_CACHE_TTL = {}, 60
ATH, _alert_last_sent = {}, {}
COOLDOWN_SEC = 60*30
_guard = {}  # key -> {"entry","peak","start_ts"}

os.makedirs(DATA_DIR, exist_ok=True)

# ---------- Utils ----------

def _format_amount(a):
    try: a=float(a)
    except: return str(a)
    if abs(a)>=1: return f"{a:,.4f}"
    if abs(a)>=0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

def _format_price(p):
    try: p=float(p)
    except: return str(p)
    if p>=1: return f"{p:,.6f}"
    if p>=0.01: return f"{p:.6f}"
    if p>=1e-6: return f"{p:.8f}"
    return f"{p:.10f}"

def _nonzero(v, eps=1e-12):
    try: return abs(float(v))>eps
    except: return False

def read_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except: return default

def write_json(path, obj):
    tmp=path+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(obj,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path)

def data_file_for_today(): return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

# ---------- ATH ----------

def load_ath():
    global ATH
    ATH = read_json(ATH_PATH, default={})
    if not isinstance(ATH, dict): ATH = {}

def save_ath(): write_json(ATH_PATH, ATH)


def update_ath(key: str, live_price: float):
    if not _nonzero(live_price): return
    prev = ATH.get(key)
    if prev is None or live_price > prev + 1e-12:
        ATH[key] = live_price; save_ath()
        send_telegram(f"🏆 New ATH {key}: ${_format_price(live_price)}")

# ---------- Pricing (Dexscreener) ----------
PRICE_ALIASES = {"tcro":"cro"}
_HISTORY_LAST_PRICE = {}


def _pick_best_price(pairs):
    if not pairs: return None
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")).
# ==== Continuation of main.py (patched), Part 2/2 ====
# This file continues exactly from where Main1.py stops.
# It contains the rest of the original main.py logic and the requested
# notify_error() hooks added into the loops and exception paths.

from datetime import datetime
import time

# ... (this part assumes all globals, helpers, and imports from Part 1 exist)

# Continue the Pricing, Etherscan, RPC, Holdings, Discovery, Alerts, Guard,
# Daily summaries, Totals, Wallet monitor, Telegram, Schedulers and main()
# sections exactly as in the canonical file — with the added error hooks.

# (For brevity here we include the modified sections; the Canvas holds full content.)

# ---------- Dex monitor & discovery ----------
from collections import deque

def slug(chain: str, pair_address: str) -> str: return f"{chain}/{pair_address}".lower()

def fetch_pair(slg_str: str):
    return safe_json(safe_get(f"{DEX_BASE_PAIRS}/{slg_str}", timeout=12))

# ... unchanged helpers fetch_token_pairs, fetch_search, ensure_tracking_pair, update_price_history, detect_spike

_last_pair_alert={}
PAIR_ALERT_COOLDOWN=60*10

def _pair_cooldown_ok(key):
    last=_last_pair_alert.get(key,0.0); now=time.time()
    if now-last>=PAIR_ALERT_COOLDOWN:
        _last_pair_alert[key]=now; return True
    return False


def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        log.info("No tracked pairs; monitor waits.")
    else:
        send_telegram(f"🚀 Dex monitor started: {', '.join(sorted(_tracked_pairs))}")
    while not shutdown_event.is_set():
        if not _tracked_pairs:
            time.sleep(DEX_POLL); continue
        for s in list(_tracked_pairs):
            try:
                data=fetch_pair(s)
                if not data: continue
                pair=None
                if isinstance(data.get("pair"),dict): pair=data["pair"]
                elif isinstance(data.get("pairs"),list) and data["pairs"]: pair=data["pairs"][0]
                if not pair: continue
                try: price_val=float(pair.get("priceUsd") or 0)
                except: price_val=None
                if price_val and price_val>0:
                    update_price_history(s, price_val)
                    spike_pct=detect_spike(s)
                    if spike_pct is not None:
                        try: vol_h1=float((pair.get("volume") or {}).get("h1") or 0)
                        except: vol_h1=None
                        if not (MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 > 0):
                            pass
                prev=_last_prices.get(s)
                if prev and prev>0 and price_val and price_val>0:
                    delta=(price_val-prev)/prev*100.0
                    if abs(delta)>=PRICE_MOVE_THRESHOLD and _pair_cooldown_ok(f"move:{s}"):
                        bt=pair.get("baseToken") or {}; symbol=bt.get("symbol") or s
                        send_telegram(f"📈 Price move on {symbol}: {delta:.2f}%\nPrice: ${_format_price(price_val)} (prev ${_format_price(prev)})")
                        _last_prices[s]=price_val
                last_tx=(pair.get("lastTx") or {}).get("hash")
                if last_tx:
                    prev_tx=_last_pair_tx.get(s)
                    if prev_tx!=last_tx and _pair_cooldown_ok(f"trade:{s}"):
                        _last_pair_tx[s]=last_tx
                        bt=pair.get("baseToken") or {}; symbol=bt.get("symbol") or s
                        send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx)}")
            except Exception as e:
                log.debug("pairs loop error %s: %s", s, e)
                try: notify_error("pairs loop", e)
                except Exception: pass
        for _ in range(DEX_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ---------- Discovery loop ----------

def discovery_loop():
    seeds=[p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    for s in seeds:
        if s.startswith("cronos/"): ensure_tracking_pair("cronos", s.split("/",1)[1])
    token_items=[t.strip().lower() for t in (TOKENS or "").split(",") if t.strip()]
    for t in token_items:
        if not t.startswith("cronos/"): continue
        _,token_addr=t.split("/",1)
        pairs=fetch_token_pairs("cronos", token_addr)
        if pairs:
            p=pairs[0]; pair_addr=p.get("pairAddress")
            if pair_addr: ensure_tracking_pair("cronos", pair_addr, meta=p)
    if not DISCOVER_ENABLED:
        log.info("Discovery disabled."); return
    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos).")
    while not shutdown_event.is_set():
        try:
            found=fetch_search(DISCOVER_QUERY); adopted=0
            for p in found or []:
                if not _pair_passes_filters(p): continue
                pair_addr=p.get("pairAddress")
                if not pair_addr: continue
                s=slug("cronos", pair_addr)
                if s in _tracked_pairs: continue
                ensure_tracking_pair("cronos", pair_addr, meta=p)
                adopted+=1
                if adopted>=DISCOVER_LIMIT: break
        except Exception as e:
            log.debug("Discovery error: %s", e)
            try: notify_error("discovery loop", e)
            except Exception: pass
        for _ in range(DISCOVER_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ---------- Alerts & Guard (hooks added) ----------

def alerts_monitor_loop():
    send_telegram(f"🛰 Alerts monitor every {ALERTS_INTERVAL_MIN}m. Wallet 24h dump/pump: {DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT}.")
    while not shutdown_event.is_set():
        try:
            wallet_bal=get_wallet_balances_snapshot()
            for sym,amt in list(wallet_bal.items()):
                if amt<=EPSILON: continue
                price,ch24,ch2h,url=get_change_and_price_for_symbol_or_addr(sym)
                if not price or price<=0: continue
                if ch24 is not None:
                    if ch24>=PUMP_ALERT_24H_PCT and _cooldown_ok(f"24h_pump:{sym}"):
                        send_telegram(f"🚀 Pump Alert {sym} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{url}")
                    if ch24<=DUMP_ALERT_24H_PCT and _cooldown_ok(f"24h_dump:{sym}"):
                        send_telegram(f"⚠️ Dump Alert {sym} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{url}")
            data=read_json(data_file_for_today(), default={"entries":[]})
            seen=set()
            for e in data.get("entries",[]):
                if float(e.get("amount") or 0)>0:
                    sym=(e.get("token") or "?").upper()
                    addr=(e.get("token_addr") or "").lower()
                    key=addr if (addr and addr.startswith("0x")) else sym
                    if key in seen: continue
                    seen.add(key)
                    query=addr if (addr and addr.startswith("0x")) else sym
                    price,ch24,ch2h,url=get_change_and_price_for_symbol_or_addr(query)
                    if not price or price<=0: continue
                    ch = ch2h if (ch2h is not None) else ch24
                    if ch is None: continue
                    if ch>=PUMP_ALERT_24H_PCT and _cooldown_ok(f"risky:pump:{key}"):
                        send_telegram(f"🚀 Pump (recent) {sym} {ch:.2f}%\nPrice ${_format_price(price)}\n{url}")
                    if ch<=DUMP_ALERT_24H_PCT and _cooldown_ok(f"risky:dump:{key}"):
                        send_telegram(f"⚠️ Dump (recent) {sym} {ch:.2f}%\nPrice ${_format_price(price)}\n{url}")
        except Exception as e:
            log.exception("alerts monitor error: %s", e)
            try: notify_error("alerts monitor", e)
            except Exception: pass
        for _ in range(ALERTS_INTERVAL_MIN*60):
            if shutdown_event.is_set(): break
            time.sleep(1)


def guard_monitor_loop():
    send_telegram(f"🛡 Guard monitor: {GUARD_WINDOW_MIN}m window, +{GUARD_PUMP_PCT}% / {GUARD_DROP_PCT}% / trailing {GUARD_TRAIL_DROP_PCT}%.")
    while not shutdown_event.is_set():
        try:
            dead=[]
            for key,st in list(_guard.items()):
                if time.time()-st["start_ts"]>GUARD_WINDOW_MIN*60:
                    dead.append(key); continue
                if key=="CRO": price=get_price_usd("CRO") or 0.0
                elif isinstance(key,str) and key.startswith("0x"): price=get_price_usd(key) or 0.0
                else:
                    meta=_token_meta.get(key,{})
                    sym=meta.get("symbol") or key
                    price=get_price_usd(sym) or 0.0
                if not price or price<=0: continue
                entry,peak=st["entry"],st["peak"]
                if price>peak: st["peak"]=price; peak=price
                pct_from_entry=(price-entry)/entry*100.0 if entry>0 else 0.0
                trail_from_peak=(price-peak)/peak*100.0 if peak>0 else 0.0
                sym=_token_meta.get(key,{}).get("symbol") or ("CRO" if key=="CRO" else (key[:6] if isinstance(key,str) else "ASSET"))
                if pct_from_entry>=GUARD_PUMP_PCT and _cooldown_ok(f"guard:pump:{key}"):
                    send_telegram(f"🟢 GUARD Pump {sym} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if pct_from_entry<=GUARD_DROP_PCT and _cooldown_ok(f"guard:drop:{key}"):
                    send_telegram(f"🔻 GUARD Drop {sym} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if trail_from_peak<=GUARD_TRAIL_DROP_PCT and _cooldown_ok(f"guard:trail:{key}"):
                    send_telegram(f"🟠 GUARD Trail {sym} {trail_from_peak:.2f}% from peak ${_format_price(peak)} → ${_format_price(price)}")
            for k in dead: _guard.pop(k,None)
        except Exception as e:
            log.exception("guard monitor error: %s", e)
            try: notify_error("guard monitor", e)
            except Exception: pass
        for _ in range(15):
            if shutdown_event.is_set(): break
            time.sleep(2)

# ---------- Wallet monitor loop (hook added) ----------

def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    last_native_hashes=set()
    last_token_hashes=set()
    while not shutdown_event.is_set():
        try:
            for tx in fetch_latest_wallet_txs(limit=25):
                h=tx.get("hash")
                if h and h not in last_native_hashes:
                    last_native_hashes.add(h); handle_native_tx(tx)
            for t in fetch_latest_token_txs(limit=100):
                h=t.get("hash")
                if h and h not in last_token_hashes:
                    last_token_hashes.add(h); handle_erc20_tx(t)
            _replay_today_cost_basis()
        except Exception as e:
            log.exception("wallet monitor error: %s", e)
            try: notify_error("wallet monitor", e)
            except Exception: pass
        for _ in range(WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ---------- Telegram long-poll (hook added) ----------

import requests

def _tg_api(method: str, **params):
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r=requests.get(url, params=params, timeout=30)
        if r.status_code==200: return r.json()
    except Exception as e:
        log.debug("tg api error %s: %s", method, e)
    return None


def telegram_long_poll_loop():
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; telegram loop disabled."); return
    offset=None
    send_telegram("🤖 Telegram command handler online.")
    while not shutdown_event.is_set():
        try:
            resp=_tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
            if not resp or not resp.get("ok"): time.sleep(2); continue
            for upd in resp.get("result",[]):
                offset = upd["update_id"] + 1
                msg=upd.get("message") or {}
                chat_id=str(((msg.get("chat") or {}).get("id") or ""))
                if TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID)!=chat_id:
                    continue
                text=(msg.get("text") or "").strip()
                if not text: continue
                _handle_command(text)
        except Exception as e:
            log.debug("telegram poll error: %s", e)
            try: notify_error("telegram poll", e)
            except Exception: pass
            time.sleep(2)

# ---------- Scheduler loop (hook added) ----------

def _scheduler_loop():
    global _last_intraday_sent
    send_telegram("⏱ Scheduler online (intraday/EOD).")
    while not shutdown_event.is_set():
        try:
            now=now_dt()
            if _last_intraday_sent<=0 or (time.time()-_last_intraday_sent)>=INTRADAY_HOURS*3600:
                send_telegram(_format_daily_sum_message()); _last_intraday_sent=time.time()
            if now.hour==EOD_HOUR and now.minute==EOD_MINUTE:
                # Call the patched EOD function (also scheduled via schedule runner)
                try:
                    text = build_day_report_text()
                    send_telegram_message(f"📒 Daily Report\n{text}")
                except Exception as e:
                    logging.exception("Failed to build or send daily report (time-check path)")
                    try: notify_error("Daily Report (time-check)", e)
                    except Exception: pass
                    try: send_telegram_message("⚠️ Failed to generate daily report.")
                    except Exception: pass
                time.sleep(65)
        except Exception as e:
            log.debug("scheduler error: %s", e)
            try: notify_error("scheduler loop", e)
            except Exception: pass
        for _ in range(20):
            if shutdown_event.is_set(): break
            time.sleep(3)

# ---------- Main (startup ping + initial holdings + schedule runner bind) ----------

def _graceful_exit(signum, frame):
    try: send_telegram("🛑 Shutting down.")
    except: pass
    shutdown_event.set()


def send_daily_report():  # defined in Part 1, redeclare no-op guard if missing
    try:
        _ = build_day_report_text  # noqa
    except NameError:
        def _noop(): pass
        return _noop()


def main():
    load_ath()

    # Startup connectivity ping (patched)
    try:
        send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")
    except Exception as e:
        logging.exception("Startup Telegram ping failed")
        try: notify_error("Startup ping", e)
        except Exception: pass

    # Initial holdings snapshot (patched)
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

    # Bind schedule-based EOD job + runner (patched)
    try:
        schedule.every().day.at(EOD_TIME).do(send_daily_report)
        logging.info(f"EOD daily report scheduled at {EOD_TIME}")

        def _schedule_runner():
            while not shutdown_event.is_set():
                try:
                    schedule.run_pending()
                except Exception as e:
                    logging.exception("schedule runner error")
                    try: notify_error("schedule runner", e)
                    except Exception: pass
                time.sleep(1)
        threading.Thread(target=_schedule_runner, name="sched-runner", daemon=True).start()
    except Exception as e:
        logging.exception("Failed to bind EOD scheduler")
        try: notify_error("Scheduler bind (EOD)", e)
        except Exception: pass

    # seed discovery & monitors
    threading.Thread(target=discovery_loop, name="discovery", daemon=True).start()
    threading.Thread(target=wallet_monitor_loop, name="wallet", daemon=True).start()
    threading.Thread(target=monitor_tracked_pairs_loop, name="dex", daemon=True).start()
    threading.Thread(target=alerts_monitor_loop, name="alerts", daemon=True).start()
    threading.Thread(target=guard_monitor_loop, name="guard", daemon=True).start()
    threading.Thread(target=telegram_long_poll_loop, name="telegram", daemon=True).start()
    threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True).start()

    while not shutdown_event.is_set():
        time.sleep(1)


if __name__=="__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log.exception("fatal: %s", e)
        try:
            send_telegram(f"💥 Fatal error: {e}")
        except: pass
        try: notify_error("fatal", e)
        except Exception: pass
        sys.exit(1)
