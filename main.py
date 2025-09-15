#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (Patched)
Focus today:
- /holdings: show all assets (CRO + ERC-20) with USD valuation
- /txs: show all today's transactions
- /report: day report with holdings & PnL

Also:
- Etherscan v2 polling (Cronos chainId=25)
- Ledger append & cost-basis replay (FIFO-ish)
- Optional alerts & watchlist hooks (if modules exist)

Requires:
  utils/http.py, telegram/api.py, reports/day_report.py,
  reports/ledger.py, reports/aggregates.py (already in your repo)
"""

from __future__ import annotations

import os, sys, time, json, threading, logging, signal
from collections import defaultdict
from datetime import datetime
from typing import Tuple, List, Dict, Any

from dotenv import load_dotenv

# --- External helpers from your repo ---
from utils.http import safe_get, safe_json
from telegram.api import send_telegram
from reports.day_report import build_day_report_text as _compose_day_report
from reports.ledger import append_ledger, replay_cost_basis_over_entries
from reports.ledger import data_file_for_today  # path helper
# (read_json local helper below)
# aggregates not strictly needed for today's features

# --- Load env / logging ---
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("main")

# --- Config / ENV (Railway/Render/Heroku friendly) ---
def _alias_env(src: str, dst: str):
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)

_alias_env("ALERTS_INTERVAL_MINUTES", "ALERTS_INTERVAL_MIN")
_alias_env("DISCOVER_REQUIRE_WCRO_QUOTE", "DISCOVER_REQUIRE_WCRO")

TZ                 = os.getenv("TZ", "Europe/Athens")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS", "")).lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API", "")
DATA_DIR           = os.getenv("DATA_DIR", "/app/data")

WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
INTRADAY_HOURS     = int(os.getenv("INTRADAY_HOURS", "3"))

# Optional (used by alerts/watchlist)
ALERTS_INTERVAL_MIN = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD","5"))
DISCOVER_MIN_LIQ_USD = float(os.getenv("DISCOVER_MIN_LIQ_USD","30000"))
DISCOVER_MIN_VOL24_USD = float(os.getenv("DISCOVER_MIN_VOL24_USD","5000"))
DISCOVER_REQUIRE_WCRO = os.getenv("DISCOVER_REQUIRE_WCRO","false").lower() in ("1","true","yes","on")

os.makedirs(DATA_DIR, exist_ok=True)

# --- TZ (local) ---
from zoneinfo import ZoneInfo
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

LOCAL_TZ = _init_tz(TZ)
def now_dt(): return datetime.now(LOCAL_TZ)
def ymd(dt=None): return (dt or now_dt()).strftime("%Y-%m-%d")

# --- Etherscan (Cronos via v2) ---
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"

# --- Pricing (Dexscreener) ---
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
PRICE_ALIASES = {"tcro": "cro"}
PRICE_CACHE: dict[str, tuple[float | None, float]] = {}
PRICE_CACHE_TTL = 60
HISTORY_LAST_PRICE: dict[str, float] = {}  # seeded from ledger reads

# --- State / positions ---
shutdown_event = threading.Event()
position_qty: Dict[str, float]  = defaultdict(float)  # key = "CRO" or 0x...
position_cost: Dict[str, float] = defaultdict(float)
EPS = 1e-12
_seen_native: set[str] = set()
_seen_token:  set[str] = set()
_last_intraday_sent = 0.0

# --- Small IO helpers ---
def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path: str, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# --- Format helpers ---
def _fmt_amount(a: float) -> str:
    try: a=float(a)
    except: return str(a)
    if abs(a)>=1: return f"{a:,.4f}"
    if abs(a)>=0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

def _fmt_price(p: float) -> str:
    try: p=float(p)
    except: return str(p)
    if p>=1: return f"{p:,.6f}"
    if p>=0.01: return f"{p:.6f}"
    if p>=1e-6: return f"{p:.8f}"
    return f"{p:.10f}"

# --- Dexscreener helpers ---
def _pick_best_price(pairs):
    if not pairs: return None
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")).lower()!="cronos": continue
            liq=float((p.get("liquidity") or {}).get("usd") or 0)
            price=float(p.get("priceUsd") or 0)
            if price<=0: continue
            if liq>best_liq: best_liq, best = liq, price
        except:  # noqa: E722
            continue
    return best

def _pairs_for_token_addr(addr: str):
    data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/cronos/{addr}", timeout=10)) or {}
    pairs = data.get("pairs") or []
    if not pairs:
        data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/{addr}", timeout=10)) or {}
        pairs = data.get("pairs") or []
    if not pairs:
        data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": addr}, timeout=10)) or {}
        pairs = data.get("pairs") or []
    return pairs

def _history_price_fallback(query_key: str, symbol_hint: str | None = None):
    if not query_key: return None
    k=query_key.strip()
    if not k: return None
    if k.startswith("0x"):
        p=HISTORY_LAST_PRICE.get(k)
        if p and p>0: return p
    sym=(symbol_hint or k)
    sym=(PRICE_ALIASES.get(sym.lower(), sym.lower())).upper()
    p=HISTORY_LAST_PRICE.get(sym)
    if p and p>0: return p
    return None

def get_price_usd(symbol_or_addr: str):
    if not symbol_or_addr: return None
    key = PRICE_ALIASES.get(symbol_or_addr.strip().lower(), symbol_or_addr.strip().lower())
    now = time.time()
    c = PRICE_CACHE.get(key)
    if c and (now - c[1] < PRICE_CACHE_TTL): return c[0]

    price=None
    try:
        if key in ("cro","wcro","w-cro","wrappedcro","wrapped cro"):
            for q in ["wcro usdt","cro usdt","cro usdc"]:
                data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
                price=_pick_best_price(data.get("pairs"))
                if price: break
        elif key.startswith("0x") and len(key)==42:
            price=_pick_best_price(_pairs_for_token_addr(key))
        else:
            for q in [key, f"{key} usdt", f"{key} wcro"]:
                data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
                price=_pick_best_price(data.get("pairs"))
                if price: break
    except:  # noqa: E722
        price=None

    if (price is None) or (not price) or (float(price)<=0):
        hist=_history_price_fallback(symbol_or_addr, symbol_hint=symbol_or_addr)
        if hist and hist>0: price=float(hist)

    PRICE_CACHE[key]=(price, now)
    return price

# --- Etherscan fetchers ---
def fetch_latest_wallet_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"txlist",
            "address":WALLET_ADDRESS,"startblock":0,"endblock":99999999,
            "page":1,"offset":limit,"sort":"desc","apikey":ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

def fetch_latest_token_txs(limit=100):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"tokentx",
            "address":WALLET_ADDRESS,"startblock":0,"endblock":99999999,
            "page":1,"offset":limit,"sort":"desc","apikey":ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

# --- Cost-basis replay (today) ---
def replay_today_cost_basis() -> float:
    position_qty.clear(); position_cost.clear()
    path=data_file_for_today()
    data=read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    total=replay_cost_basis_over_entries(position_qty, position_cost, data.get("entries",[]), eps=EPS)
    # persist realized to file for completeness
    data["realized_pnl"]=float(total)
    write_json(path, data)
    return total

# --- Build open positions (history) ---
def rebuild_open_positions_from_history() -> Tuple[Dict[str,float], Dict[str,float]]:
    pos_qty, pos_cost = defaultdict(float), defaultdict(float)
    files=[]
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"): files.append(fn)
    except Exception:
        pass
    files.sort()
    for fn in files:
        data=read_json(os.path.join(DATA_DIR,fn), default=None)
        if not isinstance(data,dict): continue
        for e in data.get("entries",[]):
            sym=(e.get("token") or "").strip()
            addr=(e.get("token_addr") or "").strip().lower()
            amt=float(e.get("amount") or 0.0)
            pr =float(e.get("price_usd") or 0.0)

            # seed history prices for fallbacks
            if pr>0:
                if addr and addr.startswith("0x"): HISTORY_LAST_PRICE[addr]=pr
                if sym: HISTORY_LAST_PRICE[sym.upper()]=pr

            key = addr if (addr and addr.startswith("0x")) else (sym.upper() or sym or "?")
            qty=pos_qty[key]; cost=pos_cost[key]
            if amt>EPS:
                pos_qty[key]=qty+amt; pos_cost[key]=cost+amt*(pr or 0.0)
            elif amt<-EPS and qty>EPS:
                sell=min(-amt, qty); avg=(cost/qty) if qty>EPS else (pr or 0.0)
                pos_qty[key]=qty-sell; pos_cost[key]=max(0.0, cost - avg*sell)
    for k,v in list(pos_qty.items()):
        if abs(v)<1e-10: pos_qty[k]=0.0
    return pos_qty, pos_cost

# --- Compute holdings now (history positions + live price) ---
def compute_holdings_now():
    pos_qty,pos_cost=rebuild_open_positions_from_history()
    total, breakdown, unrealized = 0.0, [], 0.0
    for key,amt in pos_qty.items():
        amt=max(0.0,float(amt))
        if amt<=EPS: continue
        sym=(key[:8].upper() if (isinstance(key,str) and key.startswith("0x")) else str(key))
        pr=(get_price_usd(key) if (isinstance(key,str) and key.startswith("0x")) else get_price_usd(sym)) or 0.0
        v=amt*(pr or 0.0); total+=v
        breakdown.append({"token":sym,"token_addr": key if (isinstance(key,str) and key.startswith("0x")) else None,
                          "amount":amt,"price_usd":pr,"usd_value":v})
        cost=pos_cost.get(key,0.0)
        if amt>EPS and pr>0: unrealized += (amt*pr - cost)
    breakdown.sort(key=lambda b: float(b.get("usd_value",0.0)), reverse=True)
    return total, breakdown, unrealized

# --- TX handlers (append → ledger) ---
def handle_native_tx(tx: dict):
    h=tx.get("hash")
    if not h or h in _seen_native: return
    _seen_native.add(h)
    val_raw=tx.get("value","0")
    try: amount_cro=int(val_raw)/(10**18)
    except: amount_cro=float(val_raw)

    frm=(tx.get("from") or "").lower()
    to =(tx.get("to") or "").lower()
    ts=int(tx.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts, LOCAL_TZ) if ts>0 else now_dt()

    sign= +1 if to==WALLET_ADDRESS else (-1 if frm==WALLET_ADDRESS else 0)
    if sign==0 or abs(amount_cro)<=EPS: return

    price=get_price_usd("CRO") or 0.0
    usd_value=sign*amount_cro*(price or 0.0)

    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"), "txhash": h, "type":"native",
        "token":"CRO", "token_addr": None, "amount": sign*amount_cro,
        "price_usd": price, "usd_value": usd_value, "realized_pnl": 0.0,
        "from": frm, "to": to
    })

def handle_erc20_tx(t: dict):
    h=t.get("hash") or ""
    if h in _seen_token: return
    frm=(t.get("from") or "").lower()
    to =(t.get("to") or "").lower()
    if WALLET_ADDRESS not in (frm,to): return
    _seen_token.add(h)

    token_addr=(t.get("contractAddress") or "").lower()
    symbol=t.get("tokenSymbol") or (token_addr[:8] if token_addr else "?")
    try: decimals=int(t.get("tokenDecimal") or 18)
    except: decimals=18
    val_raw=t.get("value","0")
    try: amount=int(val_raw)/(10**decimals)
    except: amount=float(val_raw)

    ts=int(t.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts, LOCAL_TZ) if ts>0 else now_dt()
    sign= +1 if to==WALLET_ADDRESS else -1

    price=(get_price_usd(token_addr) if token_addr else get_price_usd(symbol)) or 0.0
    usd_value=sign*amount*(price or 0.0)

    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h or None, "type":"erc20", "token": symbol, "token_addr": token_addr or None,
        "amount": sign*amount, "price_usd": price, "usd_value": usd_value,
        "realized_pnl": 0.0, "from": frm, "to": to
    })

# --- Wallet polling loop ---
def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    while not shutdown_event.is_set():
        try:
            for tx in fetch_latest_wallet_txs(limit=25): handle_native_tx(tx)
            for t in fetch_latest_token_txs(limit=100): handle_erc20_tx(t)
            replay_today_cost_basis()
            # Optional alerts/watchlist (if modules exist)
            try:
                # compute balances from current holdings and scan alerts
                if "scan_holdings_alerts" in globals():
                    # not used; will import below if available
                    pass
            except Exception:
                pass
        except Exception as e:
            log.exception("wallet monitor error: %s", e)
        for _ in range(WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# --- Formatting for Telegram commands ---
def format_holdings() -> str:
    tot, br, un = compute_holdings_now()
    if not br: return "📦 Κενά holdings."
    lines=["*📦 Holdings (now):*"]
    for b in br:
        lines.append(
            f"• {b['token']}: {_fmt_amount(b['amount'])}  @ ${_fmt_price(b.get('price_usd',0))}  = ${_fmt_amount(b.get('usd_value',0))}"
        )
    lines.append(f"\nΣύνολο: ${_fmt_amount(tot)}")
    if abs(un)>EPS: lines.append(f"Unrealized: ${_fmt_amount(un)}")
    return "\n".join(lines)

def format_today_txs() -> str:
    data=read_json(data_file_for_today(), default={"date": ymd(), "entries": []})
    rows=data.get("entries",[])
    if not rows: return f"🧾 Δεν υπάρχουν σημερινές κινήσεις ({ymd()})."
    lines=[f"*🧾 Συναλλαγές σήμερα ({ymd()}):*"]
    for e in rows:
      t=e.get("time","")[-8:]
      sym=(e.get("token") or "?")
      amt=float(e.get("amount") or 0.0)
      pr =float(e.get("price_usd") or 0.0)
      usd=float(e.get("usd_value") or 0.0)
      lines.append(f"• {t} — {sym}: {amt:.6f} @ ${pr:.6f} = ${usd:.2f}")
    return "\n".join(lines)

# --- Telegram long-poll ---
import requests
def _tg_api(method: str, **params):
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r=requests.get(url, params=params, timeout=30)
        if r.status_code==200: return r.json()
    except Exception:
        pass
    return None

# Optional imports (only if files exist in repo)
try:
    from core.alerts import scan_holdings_alerts  # noqa: F401
except Exception:
    scan_holdings_alerts = None  # type: ignore
try:
    from core.watchlist import run_watchlist_scan, watch_add, watch_rm, watch_list  # noqa: F401
except Exception:
    run_watchlist_scan = watch_add = watch_rm = watch_list = None  # type: ignore

def handle_command(text: str):
    low=text.strip().lower()
    if low.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor active. Use /holdings, /txs, /report.")
    elif low.startswith("/holdings") or low in ("/show_wallet_assets","/showwalletassets","/show"):
        send_telegram(format_holdings())
    elif low.startswith("/txs"):
        send_telegram(format_today_txs())
    elif low.startswith("/report"):
        tot, br, un = compute_holdings_now()
        data=read_json(data_file_for_today(), default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        txt=_compose_day_report(
            date_str=ymd(),
            entries=data.get("entries",[]),
            net_flow=float(data.get("net_usd_flow",0.0)),
            realized_today_total=float(data.get("realized_pnl",0.0)),
            holdings_total=tot,
            breakdown=br,
            unrealized=un
        )
        send_telegram(txt)
    elif low.startswith("/watch ") and watch_add:
        try:
            _, rest = low.split(" ",1)
            if rest.startswith("add "):
                send_telegram(watch_add(rest.split(" ",1)[1]))
            elif rest.startswith("rm "):
                send_telegram(watch_rm(rest.split(" ",1)[1]))
            elif rest.strip()=="list":
                send_telegram(watch_list())
            else:
                send_telegram("Usage: /watch add <query|cronos/pair> | /watch rm <query> | /watch list")
        except Exception as e:
            send_telegram(f"Watch error: {e}")
    elif low.startswith("/rules"):
        send_telegram(
            "Rules:\n"
            f"PRICE_MOVE_THRESHOLD={PRICE_MOVE_THRESHOLD}\n"
            f"DISCOVER_MIN_LIQ_USD={DISCOVER_MIN_LIQ_USD}\n"
            f"DISCOVER_MIN_VOL24_USD={DISCOVER_MIN_VOL24_USD}\n"
            f"DISCOVER_REQUIRE_WCRO={DISCOVER_REQUIRE_WCRO}\n"
            f"ALERTS_INTERVAL_MIN={ALERTS_INTERVAL_MIN}"
        )
    else:
        send_telegram("❓ Commands: /status /holdings /txs /report (/watch add|rm|list) (/rules)")

def telegram_long_poll_loop():
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; telegram loop disabled."); return
    send_telegram("🤖 Telegram command handler online.")
    offset=None
    while not shutdown_event.is_set():
        try:
            resp=_tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
            if not resp or not resp.get("ok"):
                time.sleep(2); continue
            for upd in resp.get("result",[]):
                offset = upd["update_id"] + 1
                msg=upd.get("message") or {}
                chat_id=str(((msg.get("chat") or {}).get("id") or ""))
                if TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID)!=chat_id:
                    continue
                text=(msg.get("text") or "").strip()
                if not text: continue
                handle_command(text)
        except Exception:
            time.sleep(2)

# --- Scheduler (intraday summary ping) ---
def scheduler_loop():
    global _last_intraday_sent
    while not shutdown_event.is_set():
        try:
            if (_last_intraday_sent<=0) or ((time.time()-_last_intraday_sent) >= INTRADAY_HOURS*3600):
                send_telegram(format_today_txs())
                _last_intraday_sent=time.time()
            # Optional scheduled scans
            try:
                if scan_holdings_alerts:
                    # build balances from current holdings
                    tot, br, _ = compute_holdings_now()
                    balances = { (b["token"] or "?"): float(b["amount"] or 0) for b in br }
                    scan_holdings_alerts(balances)  # cooldown respected inside
            except Exception:
                pass
            try:
                if run_watchlist_scan:
                    run_watchlist_scan()
            except Exception:
                pass
        except Exception:
            pass
        for _ in range(30):
            if shutdown_event.is_set(): break
            time.sleep(2)

# --- Entrypoint / signals ---
def _graceful_exit(signum, frame):
    try: send_telegram("🛑 Shutting down.")
    except: pass
    shutdown_event.set()

def main():
    send_telegram("🟢 Starting Cronos DeFi Sentinel (Patched).")
    threading.Thread(target=wallet_monitor_loop,    name="wallet",    daemon=True).start()
    threading.Thread(target=telegram_long_poll_loop,name="telegram",  daemon=True).start()
    threading.Thread(target=scheduler_loop,         name="scheduler", daemon=True).start()
    while not shutdown_event.is_set():
        time.sleep(1)

if __name__=="__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log.exception("fatal: %s", e)
        try: send_telegram(f"💥 Fatal error: {e}")
        except: pass
        sys.exit(1)
