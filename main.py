#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Complete Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner
Auto-discovery, PnL (realized & unrealized), intraday/EOD reports, ATH tracking, swap reconciliation.
Drop-in for Railway worker. Uses environment variables (no hardcoded secrets).
"""

# ================== IMPORTS ==================
import os
import time
import threading
import requests
import json
from collections import defaultdict
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ================== CONFIG ==================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")

CRONOSCAN_API_KEY = os.getenv("CRONOSCAN_API_KEY")  # για tx fetch
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/tokens"

INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", 3))  # default ανά 3 ώρες
EOD_TIME = os.getenv("EOD_TIME", "23:59")

# ========== Ledger (κρατάμε όλα τα trades) ==========
ledger = []  # λίστα από dicts {time, asset, qty, price, side, value}

def record_trade(asset, qty, price, side):
    """Καταγράφει κάθε trade στο ledger"""
    ledger.append({
        "time": datetime.utcnow(),
        "asset": asset.upper(),
        "qty": float(qty),
        "price": float(price),
        "side": side.upper(),  # BUY ή SELL
        "value": float(qty) * float(price)
    })

# ========== Aggregation per asset ==========
def summarize_today_per_asset():
    """Κάνει aggregation για ΣΗΜΕΡΑ ανά asset"""
    today = datetime.utcnow().date()
    summary = defaultdict(lambda: {
        "buy_qty": 0.0, "buy_value": 0.0,
        "sell_qty": 0.0, "sell_value": 0.0
    })

    for tx in ledger:
        if tx["time"].date() == today:
            s = summary[tx["asset"]]
            if tx["side"] == "BUY":
                s["buy_qty"] += tx["qty"]
                s["buy_value"] += tx["value"]
            elif tx["side"] == "SELL":
                s["sell_qty"] += tx["qty"]
                s["sell_value"] += tx["value"]

    # Υπολογισμός PnL
    results = {}
    for asset, s in summary.items():
        avg_buy = (s["buy_value"] / s["buy_qty"]) if s["buy_qty"] else 0
        avg_sell = (s["sell_value"] / s["sell_qty"]) if s["sell_qty"] else 0
        realized_pnl = s["sell_value"] - (s["sell_qty"] * avg_buy)
        results[asset] = {
            "buy_qty": round(s["buy_qty"], 4),
            "avg_buy": round(avg_buy, 6),
            "sell_qty": round(s["sell_qty"], 4),
            "avg_sell": round(avg_sell, 6),
            "realized_pnl": round(realized_pnl, 2)
        }
    return results

# Wallet monitor + Dexscreener discovery + Alerts

# --- Config ---
WALLET_ADDRESS = os.getenv("CRONOS_WALLET")  # π.χ. 0xEa53...
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_USER_ID")

CRONOSSCAN_API = os.getenv("CRONOSCAN_API")  # explorer API
DEXSCREENER_API = "https://api.dexscreener.com/latest/dex/pairs/cronos"

# ιστορικό συναλλαγών για aggregation
tx_seen = set()
wallet_state = defaultdict(lambda: {"in": 0.0, "out": 0.0, "amount": 0.0})

# --- Βοηθητικά ---
def send_telegram(msg: str):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        )
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")

def fetch_wallet_txs():
    """Τραβάει τελευταίες txs από Cronoscan"""
    try:
        url = f"https://api.cronoscan.com/api?module=account&action=tokentx&address={WALLET_ADDRESS}&sort=desc&apikey={CRONOSSCAN_API}"
        r = requests.get(url, timeout=15)
        data = r.json()
        return data.get("result", [])
    except Exception as e:
        print(f"[ERROR] Wallet fetch: {e}")
        return []

def monitor_wallet():
    """Παρακολούθηση πορτοφολιού για νέες κινήσεις"""
    txs = fetch_wallet_txs()
    for tx in txs:
        hash_ = tx["hash"]
        if hash_ in tx_seen:
            continue
        tx_seen.add(hash_)

        token = tx.get("tokenSymbol", "?")
        value = int(tx.get("value", "0")) / (10 ** int(tx.get("tokenDecimal", "18")))
        to_addr = tx.get("to", "").lower()
        from_addr = tx.get("from", "").lower()

        direction = "IN" if to_addr == WALLET_ADDRESS.lower() else "OUT"
        msg = f"[WALLET] {direction} {value:.4f} {token} | tx: {hash_[:10]}..."
        send_telegram(msg)

        # aggregation
        if direction == "IN":
            wallet_state[token]["in"] += value
            wallet_state[token]["amount"] += value
        else:
            wallet_state[token]["out"] += value
            wallet_state[token]["amount"] -= value

def scan_dexscreener():
    """Σκανάρει Dexscreener για νέα pairs/alerts"""
    try:
        r = requests.get(DEXSCREENER_API, timeout=15)
        data = r.json()
        pairs = data.get("pairs", [])
        for p in pairs[:10]:  # top 10 για παράδειγμα
            base, quote = p.get("baseToken", {}), p.get("quoteToken", {})
            base_symbol, quote_symbol = base.get("symbol"), quote.get("symbol")
            price_usd = p.get("priceUsd")
            vol24h = p.get("volume", {}).get("h24")

            if not base_symbol or not quote_symbol:
                continue

            # απλό filter ευκαιριών
            if vol24h and float(vol24h) > 100000:  # 100k 24h volume
                msg = f"[DEX] {base_symbol}/{quote_symbol} | ${price_usd} | 24h Vol: {vol24h}"
                send_telegram(msg)
    except Exception as e:
        print(f"[ERROR] Dexscreener: {e}")

def loop_monitor():
    while True:
        monitor_wallet()
        scan_dexscreener()
        time.sleep(15)  # ανά ~15s έλεγχος

# ========================= ΚΟΜΜΑΤΙ 3 =========================
# Main loop + threads για intraday/EOD

import threading
from datetime import datetime, timedelta

# --- Intraday/EOD scheduler ---
INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", 3))
EOD_HOUR = int(os.getenv("EOD_HOUR", 23))
EOD_MINUTE = int(os.getenv("EOD_MINUTE", 59))
TZ = os.getenv("TZ", "Europe/Athens")

def intraday_report():
    """Στέλνει intraday update ανά X ώρες"""
    while True:
        now = datetime.now()
        msg = f"🟡 Intraday Update\nTime: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        msg += "Per-Asset Summary:\n"
        for token, state in wallet_state.items():
            msg += f"• {token}: qty {state['amount']:.4f} | IN {state['in']:.4f} | OUT {state['out']:.4f}\n"
        send_telegram(msg)
        time.sleep(INTRADAY_HOURS * 3600)

def eod_report():
    """Στέλνει End-of-Day report στις 23:59"""
    while True:
        now = datetime.now()
        target = now.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)
        if now > target:
            target += timedelta(days=1)
        sleep_seconds = (target - now).total_seconds()
        time.sleep(sleep_seconds)

        # build EOD report
        msg = f"📒 Daily Report ({target.strftime('%Y-%m-%d')})\nTransactions summary:\n"
        for token, state in wallet_state.items():
            net_flow = state['in'] - state['out']
            msg += f"• {token}: net flow {net_flow:.4f} | total IN {state['in']:.4f} | total OUT {state['out']:.4f}\n"

        send_telegram(msg)

        # reset daily counters
        for state in wallet_state.values():
            state['in'] = 0
            state['out'] = 0

# --- Start threads ---
threading.Thread(target=loop_monitor, daemon=True).start()
threading.Thread(target=intraday_report, daemon=True).start()
threading.Thread(target=eod_report, daemon=True).start()

# Keep main alive
while True:
    time.sleep(60)

# Main entry point

def main():
    print("🚀 Starting full wallet & DEX monitor system...")

    # Threads για monitoring
    threading.Thread(target=loop_monitor, daemon=True).start()        # Κεντρικό loop (wallet + Dexscreener)
    threading.Thread(target=intraday_report, daemon=True).start()    # Intraday updates
    threading.Thread(target=eod_report, daemon=True).start()         # End-of-Day report

    # Keep main alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("🛑 Monitoring stopped manually.")

if __name__ == "__main__":
    main()
