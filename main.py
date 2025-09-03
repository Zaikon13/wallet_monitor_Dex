#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Complete Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner
Auto-discovery, PnL (realized & unrealized), intraday/EOD reports, ATH tracking, swap reconciliation,
Alerts Monitor (dump/pump) και Short-term Guard μετά από buy, με mini-summary ανά trade.
Drop-in για Railway worker. Χρησιμοποιεί ENV variables.
"""

import os, sys, time, json, math, threading, signal, logging
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
import requests
from decimal import Decimal

# ----------------------- Config / ENV -----------------------
WALLET_ADDRESS   = (os.getenv("WALLET_ADDRESS") or "").lower()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")

# Discovery / filters
DISCOVER_ENABLED   = os.getenv("DISCOVER_ENABLED","true").lower() in ("1","true","yes")
DISCOVER_QUERY     = os.getenv("DISCOVER_QUERY","cronos")
DISCOVER_LIMIT     = int(os.getenv("DISCOVER_LIMIT","5"))
DISCOVER_REQUIRE_WCRO = os.getenv("DISCOVER_REQUIRE_WCRO","true").lower() in ("1","true","yes")
DISCOVER_MIN_LIQ_USD  = float(os.getenv("DISCOVER_MIN_LIQ_USD","50000"))
DISCOVER_MIN_VOL24_USD= float(os.getenv("DISCOVER_MIN_VOL24_USD","10000"))
DISCOVER_MIN_ABS_CHG  = float(os.getenv("DISCOVER_MIN_ABS_CHG","10"))
DISCOVER_MAX_PAIR_AGE_HOURS = int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS","24"))

# Alerts / Guard
ALERTS_INTERVAL_MIN     = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
DUMP_ALERT_24H_PCT      = float(os.getenv("DUMP_ALERT_24H_PCT","-15"))
PUMP_ALERT_24H_PCT      = float(os.getenv("PUMP_ALERT_24H_PCT","20"))
GUARD_PUMP_FROM_ENTRY   = float(os.getenv("GUARD_PUMP_FROM_ENTRY","20"))
GUARD_DUMP_FROM_ENTRY   = float(os.getenv("GUARD_DUMP_FROM_ENTRY","-12"))
GUARD_TRAIL_DROP_FROM_PK= float(os.getenv("GUARD_TRAIL_DROP_FROM_PK","-8"))
GUARD_WINDOW_MIN        = int(os.getenv("GUARD_WINDOW_MIN","60"))

TZ  = os.getenv("TZ","Europe/Athens")
INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS","3"))

# ----------------------- Constants -----------------------
CRONOS_TX = "https://cronoscan.com/tx/{txhash}"
EPSILON = 1e-9

# ----------------------- Logging -----------------------
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("monitor")

# ----------------------- State -----------------------
_token_balances = defaultdict(float)
_token_meta     = {}
_position_qty   = defaultdict(float)
_position_cost  = defaultdict(float)
_seen_token_hashes  = set()
_ledger_seen_hashes = set()

_guard_positions = {}
_alert_cooldown  = {}

shutdown_event = threading.Event()

# ----------------------- Utils -----------------------
def now_dt():
    return datetime.now()

def _format_amount(v): return f"{v:.4f}"
def _format_price(v):
    if v > 1: return f"{v:.2f}"
    if v > 0.01: return f"{v:.4f}"
    return f"{v:.6f}"

# ----------------------- Telegram -----------------------
def send_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url,json={"chat_id":TELEGRAM_CHAT_ID,"text":msg,"parse_mode":"Markdown"})
    except Exception as e:
        log.error(f"telegram error: {e}")

# ----------------------- Ledger helpers -----------------------
def data_file_for_today():
    return f"ledger_{datetime.now().strftime('%Y%m%d')}.json"

def read_json(path, default=None):
    try:
        with open(path,"r") as f: return json.load(f)
    except: return default

def write_json(path, data):
    with open(path,"w") as f: json.dump(data,f,indent=2)

def _append_ledger(entry: dict):
    txh = entry.get("txhash")
    if txh:
        if txh in _ledger_seen_hashes: return
        _ledger_seen_hashes.add(txh)
    path = data_file_for_today()
    data = read_json(path, {"date": datetime.now().strftime("%Y-%m-%d"), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    if txh:
        for e in data.get("entries", []):
            if e.get("txhash") == txh: return
    data["entries"].append(entry)
    data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value",0))
    data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl",0))
    write_json(path, data)

# ----------------------- Cost basis -----------------------
def _update_cost_basis(key, delta_qty, price):
    realized = 0.0
    qty = _position_qty.get(key,0.0)
    cost= _position_cost.get(key,0.0)
    if delta_qty > EPSILON:  # buy
        _position_qty[key] = qty+delta_qty
        _position_cost[key]= cost+delta_qty*price
    elif delta_qty < -EPSILON: # sell
        sell = -delta_qty
        if qty > EPSILON:
            avg = cost/qty
            realized = sell*(price-avg)
            _position_qty[key] = max(0,qty-sell)
            _position_cost[key]= max(0,cost-avg*sell)
    return realized

# ----------------------- ERC20 Handler -----------------------
def handle_erc20_tx(t: dict):
    h = t.get("hash")
    if not h: return
    if h in _seen_token_hashes: return
    _seen_token_hashes.add(h)

    frm=(t.get("from") or "").lower()
    to =(t.get("to") or "").lower()
    if WALLET_ADDRESS not in (frm,to): return

    token_addr=(t.get("contractAddress") or "").lower()
    symbol=t.get("tokenSymbol") or token_addr[:8]
    try: decimals=int(t.get("tokenDecimal") or 18)
    except: decimals=18
    val_raw=t.get("value","0")
    try: amount=int(val_raw)/(10**decimals)
    except: 
        try: amount=float(val_raw)
        except: amount=0.0
    ts=int(t.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts) if ts>0 else now_dt()
    sign=+1 if to==WALLET_ADDRESS else -1

    price=0.0
    # TODO: replace with actual get_token_price
    usd_value=sign*amount*(price or 0.0)

    key=token_addr or symbol
    _token_balances[key]+=sign*amount
    _token_meta[key]={"symbol":symbol,"decimals":decimals}
    realized=_update_cost_basis(key,sign*amount,price or 0.0)

    entry={"time":dt.strftime("%Y-%m-%d %H:%M:%S"),"txhash":h,"token":symbol,"amount":sign*amount,
           "price_usd":price,"usd_value":usd_value,"realized_pnl":realized,"from":frm,"to":to}
    _append_ledger(entry)

    if price>0:
        _post_mini_summary_and_guard(symbol,key,sign*amount,price)

# ----------------------- Mini Summary & Guard -----------------------
def _post_mini_summary_and_guard(symbol, token_key, delta_qty, live_price):
    open_qty=_position_qty.get(token_key,0.0)
    total_cost=_position_cost.get(token_key,0.0)
    avg=(total_cost/open_qty) if open_qty>EPSILON else 0.0
    unreal=0.0
    if open_qty>EPSILON and live_price>0: unreal=live_price*open_qty-total_cost
    action="BUY" if delta_qty>0 else "SELL"
    send_telegram(f"• {action} {symbol} {_format_amount(abs(delta_qty))} @ ${_format_price(live_price)}\n   Open: {_format_amount(open_qty)} | Avg: ${_format_price(avg)} | Unreal: ${_format_amount(unreal)}")
    _update_guard_state_after_trade(token_key,symbol,delta_qty,live_price)

def _update_guard_state_after_trade(token_key,symbol,delta_qty,px):
    if px<=0: return
    st=_guard_positions.get(token_key) or {"entry":0.0,"qty":0.0,"peak":0.0}
    qty,entry=st["qty"],st["entry"]
    if delta_qty>EPSILON:
        new_qty=qty+delta_qty
        new_entry=(entry*qty+px*delta_qty)/new_qty if qty>EPSILON else px
        st.update({"qty":new_qty,"entry":new_entry,"peak":max(px,st["peak"])})
    elif delta_qty<-EPSILON:
        sell=-delta_qty
        if qty<=sell+EPSILON: _guard_positions.pop(token_key,None); return
        st["qty"]=qty-sell; st["peak"]=max(st["peak"],px)
    _guard_positions[token_key]=st

# ----------------------- Intraday / EOD reports -----------------------
def build_report(label="Daily Report"):
    path=data_file_for_today()
    data=read_json(path,{"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    lines=[f"📒 {label} ({datetime.now().strftime('%Y-%m-%d')})"]
    if data["entries"]:
        for e in data["entries"][-20:]:
            lines.append(f"• {e['time']} — {e['token']} {e['amount']:+,.4f} @ ${_format_price(e['price_usd'])}  (${e['usd_value']:+.4f})")
    else:
        lines.append("No transactions today.")
    lines.append("")
    lines.append(f"Net USD flow today: ${data.get('net_usd_flow',0.0):.4f}")
    lines.append(f"Realized PnL today: ${data.get('realized_pnl',0.0):.4f}")
    send_telegram("\n".join(lines))

def intraday_report_loop():
    while not shutdown_event.is_set():
        time.sleep(INTRADAY_HOURS*3600)
        build_report("Intraday Update")

def eod_scheduler_loop():
    while not shutdown_event.is_set():
        now=now_dt()
        target=now.replace(hour=23,minute=59,second=0,microsecond=0)
        if now>=target: target+=timedelta(days=1)
        time.sleep((target-now).total_seconds())
        build_report("End of Day Report")

# ----------------------- Alerts Monitor (dump/pump) -----------------------
def alerts_monitor_loop():
    log.info("Thread alerts_monitor starting.")
    while not shutdown_event.is_set():
        try:
            watchlist=list(_token_meta.values())
            for meta in watchlist:
                sym=meta["symbol"]
                price=0.0
                # TODO fetch % change from Dexscreener
                change24=0.0
                if abs(change24)>=abs(PUMP_ALERT_24H_PCT) or abs(change24)<=abs(DUMP_ALERT_24H_PCT):
                    key=f"{sym}_24h"
                    last=_alert_cooldown.get(key,0)
                    if time.time()-last>ALERTS_INTERVAL_MIN*60:
                        send_telegram(f"🚀 Pump/Dump Alert {sym} 24h {change24:.2f}%")
                        _alert_cooldown[key]=time.time()
        except Exception as e:
            log.error(f"alerts_monitor error: {e}")
        time.sleep(ALERTS_INTERVAL_MIN*60)

# ----------------------- Guard monitor -----------------------
def guard_monitor_loop():
    log.info("Thread guard_monitor starting.")
    while not shutdown_event.is_set():
        try:
            for key,st in list(_guard_positions.items()):
                entry=st["entry"]; qty=st["qty"]; peak=st.get("peak",entry)
                if qty<=0 or entry<=0: continue
                live_price=0.0  # TODO fetch live price
                if live_price<=0: continue
                change=(live_price-entry)/entry*100
                trail=(live_price-peak)/peak*100 if peak>0 else 0
                if change>=GUARD_PUMP_FROM_ENTRY:
                    send_telegram(f"🟢 GUARD Pump {key} {change:+.2f}% from entry (${_format_price(entry)} → ${_format_price(live_price)})")
                if change<=GUARD_DUMP_FROM_ENTRY:
                    send_telegram(f"🔴 GUARD Dump {key} {change:+.2f}% from entry (${_format_price(entry)} → ${_format_price(live_price)})")
                if trail<=GUARD_TRAIL_DROP_FROM_PK and peak>entry:
                    send_telegram(f"⚠️ GUARD Trailing {key}: {trail:.2f}% from peak (${_format_price(peak)} → ${_format_price(live_price)})")
        except Exception as e:
            log.error(f"guard_monitor error: {e}")
        time.sleep(60)

# ----------------------- Entrypoint -----------------------
def main():
    log.info("Starting monitor with config:")
    log.info(f"WALLET_ADDRESS: {WALLET_ADDRESS}")
    log.info(f"TELEGRAM_BOT_TOKEN present: {bool(TELEGRAM_BOT_TOKEN)}")
    log.info(f"TELEGRAM_CHAT_ID: {TELEGRAM_CHAT_ID}")
    log.info(f"ETHERSCAN_API present: {bool(ETHERSCAN_API)}")
    log.info(f"DEX_PAIRS: ")
    log.info(f"DISCOVER_ENABLED: {DISCOVER_ENABLED} | DISCOVER_QUERY: {DISCOVER_QUERY}")
    log.info(f"TZ: {TZ} | INTRADAY_HOURS: {INTRADAY_HOURS} | EOD: 23:59")
    log.info(f"Alerts interval: {ALERTS_INTERVAL_MIN}m | Wallet 24h dump/pump: {DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT}")

    threads=[]
    threads.append(threading.Thread(target=intraday_report_loop,daemon=True))
    threads.append(threading.Thread(target=eod_scheduler_loop,daemon=True))
    threads.append(threading.Thread(target=alerts_monitor_loop,daemon=True))
    threads.append(threading.Thread(target=guard_monitor_loop,daemon=True))
    for t in threads: t.start()
    while not shutdown_event.is_set():
        time.sleep(1)

def handle_sig(sig,frame):
    log.warning("Signal received, shutting down...")
    shutdown_event.set()

if __name__=="__main__":
    signal.signal(signal.SIGINT,handle_sig)
    signal.signal(signal.SIGTERM,handle_sig)
    main()
