#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — Fixed main.py
Removes bad import from reports.ledger (data_file_for_today, read_json) and redefines locally.
Adds: /holdings, /txs, /report, /watchlist commands.
"""
import os, sys, time, json, threading, logging, signal
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

from utils.http import safe_get, safe_json
from telegram.api import send_telegram
from reports.day_report import build_day_report_text
from reports.ledger import append_ledger, replay_cost_basis_over_entries
from reports.aggregates import aggregate_per_asset
from core.pricing import get_price_usd, HISTORY_LAST_PRICE
from core.tz import tz_init, now_dt, ymd
from core.config import settings
from core.alerts import scan_holdings_alerts
from core.watchlist import run_watchlist_scan, watch_add, watch_rm, watch_list

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("main")

shutdown_event=threading.Event()
LOCAL_TZ=tz_init(os.getenv("TZ","Europe/Athens"))
position_qty=defaultdict(float)
position_cost=defaultdict(float)
EPS=1e-12
_seen_native=set()
_seen_token=set()

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"

# --- Local IO helpers ---
def data_file_for_today():
    return os.path.join(settings.DATA_DIR, f"transactions_{ymd()}.json")

def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return default

# --- Formatters ---
def _fmt_amount(a: float) -> str:
    a=float(a)
    if abs(a)>=1: return f"{a:,.4f}"
    if abs(a)>=0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

def _fmt_price(p: float) -> str:
    p=float(p)
    if p>=1: return f"{p:,.6f}"
    if p>=0.01: return f"{p:.6f}"
    if p>=1e-6: return f"{p:.8f}"
    return f"{p:.10f}"

# --- Wallet fetchers ---
def fetch_latest_wallet_txs(limit=25):
    if not settings.WALLET_ADDRESS or not settings.ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"txlist","address":settings.WALLET_ADDRESS,
            "startblock":0,"endblock":99999999,"page":1,"offset":limit,"sort":"desc","apikey":settings.ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status",""))=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

def fetch_latest_token_txs(limit=100):
    if not settings.WALLET_ADDRESS or not settings.ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"tokentx","address":settings.WALLET_ADDRESS,
            "startblock":0,"endblock":99999999,"page":1,"offset":limit,"sort":"desc","apikey":settings.ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status",""))=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

def replay_today_cost_basis():
    position_qty.clear(); position_cost.clear()
    data=read_json(data_file_for_today(), default={"date": ymd(), "entries": []})
    total=replay_cost_basis_over_entries(position_qty, position_cost, data.get("entries",[]), eps=EPS)
    return total

# --- Holdings ---
def rebuild_open_positions_from_history():
    pos_qty, pos_cost = defaultdict(float), defaultdict(float)
    try:
        for fn in os.listdir(settings.DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"):
                data=read_json(os.path.join(settings.DATA_DIR, fn), default=None)
                if not isinstance(data,dict): continue
                for e in data.get("entries",[]):
                    sym=(e.get("token") or "").strip()
                    addr=(e.get("token_addr") or "").strip().lower()
                    amt=float(e.get("amount") or 0.0)
                    pr=float(e.get("price_usd") or 0.0)
                    key=addr if addr else sym.upper()
                    if pr>0:
                        if addr: HISTORY_LAST_PRICE[addr]=pr
                        if sym: HISTORY_LAST_PRICE[sym.upper()]=pr
                    qty=pos_qty[key]; cost=pos_cost[key]
                    if amt>EPS:
                        pos_qty[key]=qty+amt; pos_cost[key]=cost+amt*pr
                    elif amt<-EPS and qty>EPS:
                        sell=min(-amt, qty); avg=(cost/qty) if qty>EPS else pr
                        pos_qty[key]=qty-sell; pos_cost[key]=max(0.0, cost - avg*sell)
    except: pass
    return pos_qty, pos_cost

def compute_holdings_now():
    pos_qty,pos_cost=rebuild_open_positions_from_history()
    total, breakdown, unreal = 0.0, [], 0.0
    for key,amt in pos_qty.items():
        amt=max(0.0,float(amt))
        if amt<=EPS: continue
        sym=(key[:8].upper() if key.startswith("0x") else key)
        pr=get_price_usd(key) or 0.0
        v=amt*pr; total+=v
        breakdown.append({"token":sym,"token_addr": key if key.startswith("0x") else None,
                          "amount":amt,"price_usd":pr,"usd_value":v})
        cost=pos_cost.get(key,0.0)
        if amt>EPS and pr>0: unreal += (amt*pr - cost)
    breakdown.sort(key=lambda b: b["usd_value"], reverse=True)
    return total, breakdown, unreal

# --- Formatters for commands ---
def format_holdings():
    tot, br, un = compute_holdings_now()
    if not br: return "📦 Κενά holdings."
    lines=["*📦 Holdings (now):*"]
    for b in br:
        lines.append(f"• {b['token']}: {_fmt_amount(b['amount'])} @ ${_fmt_price(b['price_usd'])} = ${_fmt_amount(b['usd_value'])}")
    lines.append(f"\nΣύνολο: ${_fmt_amount(tot)}")
    if abs(un)>EPS: lines.append(f"Unrealized: ${_fmt_amount(un)}")
    return "\n".join(lines)

def format_today_txs():
    data=read_json(data_file_for_today(), default={"date": ymd(), "entries": []})
    rows=data.get("entries",[])
    if not rows: return f"🧾 Δεν υπάρχουν σημερινές κινήσεις ({ymd()})."
    lines=[f"*🧾 Συναλλαγές σήμερα ({ymd()}):*"]
    for e in rows:
        t=e.get("time","")[-8:]; sym=e.get("token"); amt=float(e.get("amount") or 0.0)
        pr=float(e.get("price_usd") or 0.0); usd=float(e.get("usd_value") or 0.0)
        lines.append(f"• {t} — {sym}: {amt:.6f} @ ${pr:.6f} = ${usd:.2f}")
    return "\n".join(lines)

# --- TX handlers ---
def handle_native_tx(tx: dict):
    h=tx.get("hash"); val_raw=tx.get("value","0")
    if not h or h in _seen_native: return
    _seen_native.add(h)
    try: amt=int(val_raw)/(10**18)
    except: amt=float(val_raw)
    frm,to=(tx.get("from") or "").lower(), (tx.get("to") or "").lower()
    ts=int(tx.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts, LOCAL_TZ) if ts>0 else now_dt()
    sign=+1 if to==settings.WALLET_ADDRESS else (-1 if frm==settings.WALLET_ADDRESS else 0)
    if sign==0: return
    price=get_price_usd("CRO") or 0.0
    usd=sign*amt*price
    append_ledger({"time":dt.strftime("%Y-%m-%d %H:%M:%S"),"txhash":h,"type":"native","token":"CRO","token_addr":None,
                   "amount":sign*amt,"price_usd":price,"usd_value":usd,"realized_pnl":0.0,"from":frm,"to":to})

def handle_erc20_tx(t: dict):
    h=t.get("hash"); frm,to=(t.get("from") or "").lower(), (t.get("to") or "").lower()
    if h in _seen_token or settings.WALLET_ADDRESS not in (frm,to): return
    _seen_token.add(h)
    token_addr=(t.get("contractAddress") or "").lower(); sym=t.get("tokenSymbol") or token_addr[:8]
    try: dec=int(t.get("tokenDecimal") or 18)
    except: dec=18
    try: amt=int(t.get("value","0"))/(10**dec)
    except: amt=float(t.get("value","0") or 0)
    ts=int(t.get("timeStamp") or 0); dt=datetime.fromtimestamp(ts, LOCAL_TZ) if ts>0 else now_dt()
    sign=+1 if to==settings.WALLET_ADDRESS else -1
    price=(get_price_usd(token_addr) if token_addr else get_price_usd(sym)) or 0.0
    usd=sign*amt*price
    append_ledger({"time":dt.strftime("%Y-%m-%d %H:%M:%S"),"txhash":h,"type":"erc20","token":sym,"token_addr":token_addr,
                   "amount":sign*amt,"price_usd":price,"usd_value":usd,"realized_pnl":0.0,"from":frm,"to":to})

# --- Loops ---
def wallet_monitor_loop():
    while not shutdown_event.is_set():
        for tx in fetch_latest_wallet_txs(): handle_native_tx(tx)
        for t in fetch_latest_token_txs(): handle_erc20_tx(t)
        replay_today_cost_basis()
        try:
            tot,br,_=compute_holdings_now()
            balances={b["token"]:b["amount"] for b in br}
            scan_holdings_alerts(balances)
            run_watchlist_scan()
        except: pass
        for _ in range(settings.WALLET_POLL):
            if shutdown_event.is_set(): break; time.sleep(1)

# --- Telegram ---
import requests
def _tg_api(method: str, **params):
    url=f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r=requests.get(url, params=params, timeout=30)
        if r.status_code==200: return r.json()
    except: return None

def handle_command(text: str):
    low=text.strip().lower()
    if low.startswith("/status"): send_telegram("✅ Running. /holdings /txs /report /watch")
    elif low.startswith("/holdings"): send_telegram(format_holdings())
    elif low.startswith("/txs"): send_telegram(format_today_txs())
    elif low.startswith("/report"):
        tot,br,un=compute_holdings_now(); d=read_json(data_file_for_today(), default={"date":ymd(),"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
        txt=build_day_report_text(date_str=ymd(), entries=d["entries"], net_flow=d["net_usd_flow"], realized_today_total=d["realized_pnl"], holdings_total=tot, breakdown=br, unrealized=un)
        send_telegram(txt)
    elif low.startswith("/watch "):
        _, rest = low.split(" ",1)
        if rest.startswith("add "): send_telegram(watch_add(rest.split(" ",1)[1]))
        elif rest.startswith("rm "): send_telegram(watch_rm(rest.split(" ",1)[1]))
        elif rest.strip()=="list": send_telegram(watch_list())
        else: send_telegram("Usage: /watch add <query> | /watch rm <query> | /watch list")
    else: send_telegram("❓ Commands: /status /holdings /txs /report /watch ...")

def telegram_long_poll_loop():
    if not settings.TELEGRAM_BOT_TOKEN: return
    send_telegram("🤖 Telegram online.")
    offset=None
    while not shutdown_event.is_set():
        try:
            resp=_tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
            if not resp or not resp.get("ok"): time.sleep(2); continue
            for upd in resp.get("result",[]):
                offset=upd["update_id"]+1; msg=upd.get("message") or {}
                chat_id=str((msg.get("chat") or {}).get("id") or "")
                if settings.TELEGRAM_CHAT_ID and str(settings.TELEGRAM_CHAT_ID)!=chat_id: continue
                txt=(msg.get("text") or "").strip()
                if txt: handle_command(txt)
        except: time.sleep(2)

# --- Entrypoint ---
def _graceful_exit(signum, frame): shutdown_event.set()

def main():
    send_telegram("🟢 Cronos DeFi Sentinel starting.")
    threading.Thread(target=wallet_monitor_loop, daemon=True).start()
    threading.Thread(target=telegram_long_poll_loop, daemon=True).start()
    while not shutdown_event.is_set(): time.sleep(1)

if __name__=="__main__":
    signal.signal(signal.SIGINT,_graceful_exit); signal.signal(signal.SIGTERM,_graceful_exit)
    main()
