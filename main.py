#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — Patched main.py (no external data_file_for_today/read_json import)
Adds: /holdings, /txs, /report, /watch commands; alerts/watchlist hooks (if present).
"""

from __future__ import annotations
import os, sys, time, json, threading, logging, signal
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv

# ── Repo helpers ──────────────────────────────────────────────────────────────
from utils.http import safe_get, safe_json
from telegram.api import send_telegram
from reports.day_report import build_day_report_text
from reports.ledger import append_ledger, replay_cost_basis_over_entries
from core.pricing import get_price_usd, HISTORY_LAST_PRICE
from core.tz import tz_init, now_dt, ymd
from core.config import settings

# Optional hooks (keep running even if missing)
try:
    from core.alerts import scan_holdings_alerts
except Exception:
    scan_holdings_alerts = None  # type: ignore
try:
    from core.watchlist import run_watchlist_scan, watch_add, watch_rm, watch_list
except Exception:
    run_watchlist_scan = watch_add = watch_rm = watch_list = None  # type: ignore

# ── Bootstrap ────────────────────────────────────────────────────────────────
load_dotenv()
os.makedirs(settings.DATA_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("main")

shutdown_event = threading.Event()
LOCAL_TZ = tz_init(os.getenv("TZ", "Europe/Athens"))

position_qty: dict[str, float] = defaultdict(float)  # key = "CRO" or 0x..
position_cost: dict[str, float] = defaultdict(float)
EPS = 1e-12
_seen_native: set[str] = set()
_seen_token: set[str]  = set()

# ── Etherscan (Cronos) ───────────────────────────────────────────────────────
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"

# ── Local IO helpers (fix for ImportError) ───────────────────────────────────
def data_file_for_today() -> str:
    return os.path.join(settings.DATA_DIR, f"transactions_{ymd()}.json")

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

# ── Formatters ───────────────────────────────────────────────────────────────
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

# ── Etherscan fetchers ───────────────────────────────────────────────────────
def fetch_latest_wallet_txs(limit=25):
    if not settings.WALLET_ADDRESS or not settings.ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"txlist",
            "address":settings.WALLET_ADDRESS,"startblock":0,"endblock":99999999,
            "page":1,"offset":limit,"sort":"desc","apikey":settings.ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

def fetch_latest_token_txs(limit=100):
    if not settings.WALLET_ADDRESS or not settings.ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"tokentx",
            "address":settings.WALLET_ADDRESS,"startblock":0,"endblock":99999999,
            "page":1,"offset":limit,"sort":"desc","apikey":settings.ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

# ── Cost-basis replay (today) ────────────────────────────────────────────────
def replay_today_cost_basis() -> float:
    position_qty.clear(); position_cost.clear()
    path=data_file_for_today()
    data=read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    total=replay_cost_basis_over_entries(position_qty, position_cost, data.get("entries",[]), eps=EPS)
    data["realized_pnl"]=float(total); write_json(path, data)  # keep file consistent
    return total

# ── Holdings from history + live pricing ─────────────────────────────────────
def rebuild_open_positions_from_history():
    pos_qty, pos_cost = defaultdict(float), defaultdict(float)
    files=[]
    try:
        for fn in os.listdir(settings.DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"): files.append(fn)
    except Exception:
        pass
    files.sort()
    for fn in files:
        path=os.path.join(settings.DATA_DIR, fn)
        data=read_json(path, default=None)
        if not isinstance(data,dict): continue
        for e in data.get("entries",[]):
            sym=(e.get("token") or "").strip()
            addr=(e.get("token_addr") or "").strip().lower()
            amt=float(e.get("amount") or 0.0)
            pr =float(e.get("price_usd") or 0.0)

            # seed fallback prices
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

# ── Formatters for Telegram commands ─────────────────────────────────────────
def format_holdings():
    tot, br, un = compute_holdings_now()
    if not br: return "📦 Κενά holdings."
    lines=["*📦 Holdings (now):*"]
    for b in br:
        lines.append(f"• {b['token']}: {_fmt_amount(b['amount'])}  @ ${_fmt_price(b.get('price_usd',0))}  = ${_fmt_amount(b.get('usd_value',0))}")
    lines.append(f"\nΣύνολο: ${_fmt_amount(tot)}")
    if abs(un)>EPS: lines.append(f"Unrealized: ${_fmt_amount(un)}")
    return "\n".join(lines)

def format_today_txs():
    data=read_json(data_file_for_today(), default={"date": ymd(), "entries": []})
    rows=data.get("entries",[])
    if not rows: return f"🧾 Δεν υπάρχουν σημερινές κινήσεις ({ymd()})."
    lines=[f"*🧾 Συναλλαγές σήμερα ({ymd()}):*"]
    for e in rows:
        t=e.get("time","")[-8:]; sym=(e.get("token") or "?"); amt=float(e.get("amount") or 0.0)
        pr=float(e.get("price_usd") or 0.0); usd=float(e.get("usd_value") or 0.0)
        lines.append(f"• {t} — {sym}: {amt:.6f} @ ${pr:.6f} = ${usd:.2f}")
    return "\n".join(lines)

# ── TX handlers ──────────────────────────────────────────────────────────────
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

    sign= +1 if to==settings.WALLET_ADDRESS else (-1 if frm==settings.WALLET_ADDRESS else 0)
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
    if settings.WALLET_ADDRESS not in (frm,to): return
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
    sign= +1 if to==settings.WALLET_ADDRESS else -1

    price=(get_price_usd(token_addr) if token_addr else get_price_usd(symbol)) or 0.0
    usd_value=sign*amount*(price or 0.0)

    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h or None, "type":"erc20", "token": symbol, "token_addr": token_addr or None,
        "amount": sign*amount, "price_usd": price, "usd_value": usd_value,
        "realized_pnl": 0.0, "from": frm, "to": to
    })

# ── Loops ────────────────────────────────────────────────────────────────────
def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    while not shutdown_event.is_set():
        try:
            for tx in fetch_latest_wallet_txs(limit=25): handle_native_tx(tx)
            for t in fetch_latest_token_txs(limit=100): handle_erc20_tx(t)
            replay_today_cost_basis()

            # optional: alerts/watchlist
            try:
                if scan_holdings_alerts:
                    _tot, _br, _ = compute_holdings_now()
                    balances = { (b["token"] or "?"): float(b["amount"] or 0) for b in _br }
                    scan_holdings_alerts(balances)
            except Exception:
                pass
            try:
                if run_watchlist_scan:
                    run_watchlist_scan()
            except Exception:
                pass

        except Exception as e:
            log.exception("wallet monitor error: %s", e)
        for _ in range(settings.WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ── Telegram long-poll ───────────────────────────────────────────────────────
import requests
def _tg_api(method: str, **params):
    url=f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r=requests.get(url, params=params, timeout=30)
        if r.status_code==200: return r.json()
    except Exception:
        pass
    return None

def format_help():
    return ("❓ Commands: /status /holdings /txs /report "
            + ("/watch add|rm|list " if watch_add else ""))

def handle_command(text: str):
    low=text.strip().lower()
    if low.startswith("/status"):
        send_telegram("✅ Running. Use /holdings /txs /report" + (" /watch" if watch_add else ""))
    elif low.startswith("/holdings"):
        send_telegram(format_holdings())
    elif low.startswith("/txs"):
        send_telegram(format_today_txs())
    elif low.startswith("/report"):
        tot, br, un = compute_holdings_now()
        d=read_json(data_file_for_today(), default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        txt=build_day_report_text(date_str=ymd(), entries=d.get("entries",[]), net_flow=float(d.get("net_usd_flow",0.0)),
                                  realized_today_total=float(d.get("realized_pnl",0.0)), holdings_total=tot, breakdown=br, unrealized=un)
        send_telegram(txt)
    elif low.startswith("/watch ") and watch_add:
        try:
            _, rest = low.split(" ",1)
            if rest.startswith("add "): send_telegram(watch_add(rest.split(" ",1)[1]))
            elif rest.startswith("rm "): send_telegram(watch_rm(rest.split(" ",1)[1]))
            elif rest.strip()=="list": send_telegram(watch_list())
            else: send_telegram("Usage: /watch add <query|cronos/pair> | /watch rm <query> | /watch list")
        except Exception as e:
            send_telegram(f"Watch error: {e}")
    else:
        send_telegram(format_help())

def telegram_long_poll_loop():
    if not settings.TELEGRAM_BOT_TOKEN:
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
                if settings.TELEGRAM_CHAT_ID and str(settings.TELEGRAM_CHAT_ID)!=chat_id:
                    continue
                text=(msg.get("text") or "").strip()
                if not text: continue
                handle_command(text)
        except Exception:
            time.sleep(2)

# ── Scheduler (optional mini) ────────────────────────────────────────────────
_last_intraday_sent=0.0
def scheduler_loop():
    global _last_intraday_sent
    while not shutdown_event.is_set():
        try:
            if _last_intraday_sent<=0 or (time.time()-_last_intraday_sent)>=settings.INTRADAY_HOURS*3600:
                send_telegram(format_today_txs()); _last_intraday_sent=time.time()
        except Exception:
            pass
        for _ in range(30):
            if shutdown_event.is_set(): break
            time.sleep(2)

# ── Entrypoint ───────────────────────────────────────────────────────────────
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
