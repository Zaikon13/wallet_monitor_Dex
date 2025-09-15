#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cronos DeFi Sentinel — Modular main
Keeps <1000 lines; orchestrates modules.
"""
from __future__ import annotations

import os, time, json, signal, logging, threading
from collections import defaultdict, deque
from datetime import datetime

from dotenv import load_dotenv

from core.config import settings
from core.tz import tz_init, now_dt, ymd
from core.pricing import get_price_usd, get_change_and_price_for_symbol_or_addr
from core.rpc import CronosRPC
from core.holdings import rebuild_open_positions_from_history, compute_holdings_from_positions
from reports.ledger import append_ledger, replay_cost_basis_over_entries, data_file_for_today, read_json
from reports.day_report import build_day_report_text
from reports.aggregates import aggregate_per_asset
from telegram.api import send_telegram

load_dotenv()
os.makedirs(settings.DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("main")

# --- Globals (runtime state) ---
shutdown_event=threading.Event()
LOCAL_TZ=tz_init(os.getenv("TZ","Europe/Athens"))

position_qty=defaultdict(float)
position_cost=defaultdict(float)
EPS=1e-12

_seen_native=set()
_seen_token=set()

# --- Etherscan lite (HTTP v2) ---
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"

from utils.http import safe_get, safe_json

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

# --- Cost-basis replay for today ---

def replay_today_cost_basis():
    position_qty.clear(); position_cost.clear()
    data=read_json(data_file_for_today(), default={"date": ymd(), "entries": []})
    total=replay_cost_basis_over_entries(position_qty, position_cost, data.get("entries",[]), eps=EPS)
    return total

# --- Holdings ---

rpc = CronosRPC(settings.CRONOS_RPC_URL) if settings.CRONOS_RPC_URL else None

def compute_holdings_merged():
    # History-based positions (rebuild)
    pos_qty, pos_cost = rebuild_open_positions_from_history(settings.DATA_DIR)

    # Add on-chain CRO + discovered ERC-20 balances
    total, breakdown, unreal = compute_holdings_from_positions(pos_qty, pos_cost)
    return total, breakdown, unreal

# --- Native / ERC-20 handlers ---

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


def handle_native_tx(tx: dict):
    h=tx.get("hash");
    if not h or h in _seen_native: return
    _seen_native.add(h)
    val_raw=tx.get("value","0")
    try: amount_cro=int(val_raw)/(10**18)
    except: amount_cro=float(val_raw)
    frm=(tx.get("from") or "").lower(); to=(tx.get("to") or "").lower()
    ts=int(tx.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts, LOCAL_TZ)
    sign= +1 if to==settings.WALLET_ADDRESS else (-1 if frm==settings.WALLET_ADDRESS else 0)
    if sign==0 or abs(amount_cro)<=EPS: return
    price=get_price_usd("CRO") or 0.0
    usd_value=sign*amount_cro*price

    realized = 0.0  # will be recomputed in replay_today_cost_basis()
    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"), "txhash": h, "type":"native",
        "token":"CRO", "token_addr": None, "amount": sign*amount_cro,
        "price_usd": price, "usd_value": usd_value, "realized_pnl": realized,
        "from": frm, "to": to
    })
    link=CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\nHash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount_cro:.6f} CRO\nPrice: ${_fmt_price(price)}\nUSD value: ${_fmt_amount(usd_value)}"
    )


def handle_erc20_tx(t: dict):
    h=t.get("hash") or ""; frm=(t.get("from") or "").lower(); to=(t.get("to") or "").lower()
    if h in _seen_token or settings.WALLET_ADDRESS not in (frm,to): return
    _seen_token.add(h)
    token_addr=(t.get("contractAddress") or "").lower()
    symbol=t.get("tokenSymbol") or (token_addr[:8] if token_addr else "?")
    try: decimals=int(t.get("tokenDecimal") or 18)
    except: decimals=18
    val_raw=t.get("value","0")
    try: amount=int(val_raw)/(10**decimals)
    except: amount=float(val_raw)

    ts=int(t.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts, LOCAL_TZ)
    sign= +1 if to==settings.WALLET_ADDRESS else -1

    price=(get_price_usd(token_addr) if token_addr else get_price_usd(symbol)) or 0.0
    usd_value=sign*amount*price

    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"), "txhash": h or None, "type":"erc20",
        "token": symbol, "token_addr": token_addr or None,
        "amount": sign*amount, "price_usd": price, "usd_value": usd_value,
        "realized_pnl": 0.0, "from": frm, "to": to
    })

    link=CRONOS_TX.format(txhash=h); direction="IN" if sign>0 else "OUT"
    send_telegram(
        f"Token TX ({direction}) {symbol}\nHash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {symbol}\nPrice: ${_fmt_price(price)}\nUSD value: ${_fmt_amount(usd_value)}"
    )

# --- Loops ---

def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    while not shutdown_event.is_set():
        try:
            for tx in fetch_latest_wallet_txs(limit=25):
                handle_native_tx(tx)
            for t in fetch_latest_token_txs(limit=100):
                handle_erc20_tx(t)
            replay_today_cost_basis()
        except Exception as e:
            log.exception("wallet monitor error: %s", e)
        for _ in range(settings.WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)


def summarize_today_per_asset():
    data=read_json(data_file_for_today(), default={"date": ymd(), "entries": []})
    entries=data.get("entries",[])
    agg={}
    for e in entries:
        sym=(e.get("token") or "?").upper(); addr=(e.get("token_addr") or "").lower()
        key=addr if addr.startswith("0x") else sym
        rec=agg.get(key)
        if not rec:
            rec={"symbol":sym,"token_addr":addr or None,"buy_qty":0.0,"sell_qty":0.0,
                 "net_qty_today":0.0,"net_flow_today":0.0,"realized_today":0.0}
            agg[key]=rec
        amt=float(e.get("amount") or 0.0); usd=float(e.get("usd_value") or 0.0); pr=float(e.get("price_usd") or 0.0)
        rp=float(e.get("realized_pnl") or 0.0)
        if amt>0: rec["buy_qty"]+=amt
        if amt<0: rec["sell_qty"]+=-amt
        rec["net_qty_today"]+=amt
        rec["net_flow_today"]+=usd
        rec["realized_today"]+=rp
    return sorted(agg.values(), key=lambda r: abs(r["net_flow_today"]), reverse=True)


def format_daily_sum_message():
    per=summarize_today_per_asset()
    if not per: return f"🧾 Δεν υπάρχουν σημερινές κινήσεις ({ymd()})."
    tot_real=sum(float(r.get("realized_today",0.0)) for r in per)
    tot_flow=sum(float(r.get("net_flow_today",0.0)) for r in per)
    lines=[f"*🧾 Daily PnL (Today {ymd()}):*"]
    for r in per:
        tok=r.get("symbol") or "?"; flow=float(r.get("net_flow_today",0.0)); real=float(r.get("realized_today",0.0))
        qty=float(r.get("net_qty_today",0.0))
        lines.append(f"• {tok}: realized ${_fmt_amount(real)} | flow ${_fmt_amount(flow)} | qty {_fmt_amount(qty)}")
    lines.append("")
    lines.append(f"*Σύνολο realized σήμερα:* ${_fmt_amount(tot_real)}")
    lines.append(f"*Σύνολο net flow σήμερα:* ${_fmt_amount(tot_flow)}")
    return "\n".join(lines)

# Totals (today|month|all)

def iter_ledger_files_for_scope(scope: str):
    files=[]
    if scope=="today":
        files=[f"transactions_{ymd()}.json"]
    elif scope=="month":
        pref=ymd()[:7]
        try:
            for fn in os.listdir(settings.DATA_DIR):
                if fn.startswith(f"transactions_{pref}") and fn.endswith(".json"): files.append(fn)
        except Exception:
            pass
    else:
        try:
            for fn in os.listdir(settings.DATA_DIR):
                if fn.startswith("transactions_") and fn.endswith(".json"): files.append(fn)
        except Exception:
            pass
    files.sort()
    return [os.path.join(settings.DATA_DIR,fn) for fn in files]

import os

def load_entries_for_totals(scope: str):
    entries=[]
    for path in iter_ledger_files_for_scope(scope):
        data=read_json(path, default=None)
        if not isinstance(data,dict): continue
        for e in data.get("entries",[]):
            sym=(e.get("token") or "?").upper(); amt=float(e.get("amount") or 0.0)
            usd=float(e.get("usd_value") or 0.0); realized=float(e.get("realized_pnl") or 0.0)
            side="IN" if amt>0 else "OUT"
            entries.append({"asset":sym,"side":side,"qty":abs(amt),"usd":usd,"realized_usd":realized})
    return entries


def format_totals(scope: str):
    scope=(scope or "all").lower()
    rows=aggregate_per_asset(load_entries_for_totals(scope))
    if not rows: return f"📊 Totals per Asset — {scope.capitalize()}: (no data)"
    lines=[f"📊 Totals per Asset — {scope.capitalize()}:"]
    for i,r in enumerate(rows,1):
        lines.append(
            f"{i}. {r['asset']}  IN: {_fmt_amount(r['in_qty'])} (${_fmt_amount(r['in_usd'])}) | "
            f"OUT: {_fmt_amount(r['out_qty'])} (${_fmt_amount(r['out_usd'])}) | REAL: ${_fmt_amount(r['realized_usd'])}"
        )
    lines.append(f"\nΣύνολο realized: ${_fmt_amount(sum(float(x['realized_usd']) for x in rows))}")
    return "\n".join(lines)

# Telegram commands (polling)
import requests

def _tg_api(method: str, **params):
    url=f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r=requests.get(url, params=params, timeout=30)
        if r.status_code==200: return r.json()
    except Exception:
        pass
    return None


def handle_command(text: str):
    low=text.strip().lower()
    if low.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor, Alerts & Scheduler active.")
    elif low.startswith("/diag"):
        send_telegram(
            "🔧 Diagnostics\n"
            f"WALLET: {settings.WALLET_ADDRESS}\n"
            f"CRONOSRPCURL set: {bool(settings.CRONOS_RPC_URL)}\n"
            f"Etherscan key: {bool(settings.ETHERSCAN_API)}\n"
            f"TZ=Europe/Athens INTRADAYHOURS={settings.INTRADAY_HOURS} EOD={settings.EOD_HOUR:02d}:{settings.EOD_MINUTE:02d}\n"
        )
    elif low.startswith("/rescan"):
        send_telegram("🔄 (Modular v1) Rescan is not implemented in this cut.")
    elif low.startswith("/holdings") or low in ("/show_wallet_assets","/showwalletassets","/show"):
        tot, br, un = compute_holdings_merged()
        if not br:
            send_telegram("📦 Κενά holdings.")
        else:
            lines=["*📦 Holdings (merged):*"]
            for b in br:
                lines.append(f"• {b['token']}: {_fmt_amount(b['amount'])}  @ ${_fmt_price(b.get('price_usd',0))}  = ${_fmt_amount(b.get('usd_value',0))}")
            lines.append(f"\nΣύνολο: ${_fmt_amount(tot)}")
            if abs(un)>EPS: lines.append(f"Unrealized: ${_fmt_amount(un)}")
            send_telegram("\n".join(lines))
    elif low.startswith("/dailysum") or low.startswith("/showdaily"):
        send_telegram(format_daily_sum_message())
    elif low.startswith("/report"):
        # rebuild holdings for report
        tot, br, un = compute_holdings_merged()
        data=read_json(data_file_for_today(), default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        txt=build_day_report_text(date_str=ymd(), entries=data.get("entries",[]), net_flow=float(data.get("net_usd_flow",0.0)),
                                  realized_today_total=float(data.get("realized_pnl",0.0)), holdings_total=tot, breakdown=br, unrealized=un, data_dir=settings.DATA_DIR)
        send_telegram(txt)
    elif low.startswith("/totals"):
        parts=low.split(); scope=parts[1] if len(parts)>1 and parts[1] in ("today","month","all") else "all"
        send_telegram(format_totals(scope))
    elif low.startswith("/totalstoday"):
        send_telegram(format_totals("today"))
    elif low.startswith("/totalsmonth"):
        send_telegram(format_totals("month"))
    elif low.startswith("/pnl"):
        parts=low.split(); scope=parts[1] if len(parts)>1 and parts[1] in ("today","month","all") else "all"
        send_telegram(format_totals(scope))
    else:
        send_telegram("❓ Commands: /status /diag /rescan /holdings /show /dailysum /report /totals [today|month|all] /totalstoday /totalsmonth /pnl [scope]")


def telegram_long_poll_loop():
    if not settings.TELEGRAM_BOT_TOKEN:
        return
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
        except Exception as e:
            time.sleep(2)

# --- Scheduler (intraday / EOD) ---
_last_intraday_sent=0.0

def scheduler_loop():
    global _last_intraday_sent
    send_telegram("⏱ Scheduler online (intraday/EOD).")
    while not shutdown_event.is_set():
        try:
            now=now_dt()
            if _last_intraday_sent<=0 or (time.time()-_last_intraday_sent)>=settings.INTRADAY_HOURS*3600:
                send_telegram(format_daily_sum_message()); _last_intraday_sent=time.time()
            if now.hour==settings.EOD_HOUR and now.minute==settings.EOD_MINUTE:
                tot, br, un = compute_holdings_merged()
                data=read_json(data_file_for_today(), default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
                txt=build_day_report_text(date_str=ymd(), entries=data.get("entries",[]), net_flow=float(data.get("net_usd_flow",0.0)),
                                          realized_today_total=float(data.get("realized_pnl",0.0)), holdings_total=tot, breakdown=br, unrealized=un, data_dir=settings.DATA_DIR)
                send_telegram(txt)
                time.sleep(65)
        except Exception:
            pass
        for _ in range(20):
            if shutdown_event.is_set(): break
            time.sleep(3)

# --- Entrypoint ---

def _graceful_exit(signum, frame):
    try: send_telegram("🛑 Shutting down.")
    except: pass
    shutdown_event.set()


def main():
    send_telegram("🟢 Starting Cronos DeFi Sentinel (Modular v1).")
    threading.Thread(target=wallet_monitor_loop, name="wallet", daemon=True).start()
    threading.Thread(target=telegram_long_poll_loop, name="telegram", daemon=True).start()
    threading.Thread(target=scheduler_loop, name="scheduler", daemon=True).start()
    while not shutdown_event.is_set():
        time.sleep(1)

if __name__=="__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    main()
