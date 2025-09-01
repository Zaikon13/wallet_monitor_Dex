# main_part1.py
import os
import time
import threading
import requests
import json
from datetime import datetime, timedelta
from collections import defaultdict

# ================= Environment Variables =================
DEX_POLL = int(os.getenv("DEX_POLL", 60))
ETHERSCAN_API = os.getenv("ETHERSCAN_API")
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", 5))
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
WALLET_POLL = int(os.getenv("WALLET_POLL", 15))
DISCOVER_ENABLED = os.getenv("DISCOVER_ENABLED", "false").lower() == "true"
DISCOVER_QUERY = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT = int(os.getenv("DISCOVER_LIMIT", 10))
DISCOVER_POLL = int(os.getenv("DISCOVER_POLL", 120))
TOKENS = os.getenv("TOKENS", "").split(",") if os.getenv("TOKENS") else []
PRICE_WINDOW = int(os.getenv("PRICE_WINDOW", 3))
SPIKE_THRESHOLD = float(os.getenv("SPIKE_THRESHOLD", 8))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT", 0))
TZ = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", 3))
EOD_HOUR = int(os.getenv("EOD_HOUR", 23))
EOD_MINUTE = int(os.getenv("EOD_MINUTE", 59))

# ================== Globals ==================
wallet_tx_cache = []
asset_summary = defaultdict(lambda: {"in": 0.0, "out": 0.0, "price": 0.0})
tracked_pairs = {}
intraday_log = []

# ================== Helpers ==================
def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message})
        if not resp.ok:
            print(f"Telegram send failed: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Telegram exception: {e}")

def retry_request(url, params=None, headers=None, max_retries=5, delay=2):
    attempt = 0
    while attempt < max_retries:
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code in [404, 429]:
                print(f"HTTP non-ok: {resp.status_code}, retrying in {delay}s...")
                time.sleep(delay)
                attempt += 1
                delay *= 2
            else:
                print(f"HTTP error {resp.status_code}")
                return None
        except Exception as e:
            print(f"Request exception: {e}, retrying...")
            time.sleep(delay)
            attempt += 1
            delay *= 2
    return None

def update_asset_summary(tx_type, symbol, qty, price):
    if tx_type == "IN":
        asset_summary[symbol]["in"] += qty
    elif tx_type == "OUT":
        asset_summary[symbol]["out"] += qty
    asset_summary[symbol]["price"] = price

def format_asset_summary():
    lines = []
    for symbol, data in asset_summary.items():
        net_qty = data["in"] - data["out"]
        net_usd = net_qty * data["price"]
        lines.append(f"{symbol}: IN {data['in']:.4f} | OUT {data['out']:.4f} | REMAIN {net_qty:.4f} | price ${data['price']:.6f} | USD ${net_usd:.2f}")
    return "\n".join(lines)

# ================== Wallet Monitor ==================
def fetch_wallet_txs():
    url = f"https://api.cronoscan.com/api"
    params = {
        "module": "account",
        "action": "txlist",
        "address": WALLET_ADDRESS,
        "startblock": 0,
        "endblock": 99999999,
        "sort": "asc",
        "apikey": ETHERSCAN_API
    }
    data = retry_request(url, params=params)
    if data and data.get("status") == "1":
        return data.get("result", [])
    return []

def wallet_monitor_loop():
    global wallet_tx_cache
    while True:
        txs = fetch_wallet_txs()
        new_txs = [tx for tx in txs if tx["hash"] not in wallet_tx_cache]
        for tx in new_txs:
            wallet_tx_cache.append(tx["hash"])
            symbol = tx.get("tokenSymbol", "CRO")
            amount = float(tx.get("value", 0)) / (10 ** int(tx.get("tokenDecimal", 18)))
            price = float(tx.get("price", 0)) if "price" in tx else 0.0
            tx_type = "IN" if tx["to"].lower() == WALLET_ADDRESS.lower() else "OUT"
            update_asset_summary(tx_type, symbol, amount, price)
            intraday_log.append((datetime.now(), tx_type, symbol, amount, price))
            send_telegram(f"{tx_type} {symbol} {amount:.4f} @ ${price:.6f}")
        time.sleep(WALLET_POLL)

# main_part2.py (συνέχεια του Part 1)

# ================== Dex Discovery ==================
def discover_pairs_loop():
    global tracked_pairs
    if not DISCOVER_ENABLED:
        return
    while True:
        url = f"https://api.dexscreener.com/latest/dex/pairs/{DISCOVER_QUERY}"
        data = retry_request(url)
        if data and "pairs" in data:
            for pair in data["pairs"][:DISCOVER_LIMIT]:
                addr = pair.get("address")
                if addr and addr not in tracked_pairs:
                    tracked_pairs[addr] = pair
                    send_telegram(f"🆕 Now monitoring pair: {pair.get('name')} ({pair.get('url')})")
        time.sleep(DISCOVER_POLL)

# ================== Intraday & EOD Reporting ==================
def intraday_report_loop():
    while True:
        if intraday_log:
            now = datetime.now()
            window_start = now - timedelta(hours=INTRADAY_HOURS)
            recent_tx = [tx for tx in intraday_log if tx[0] >= window_start]
            if recent_tx:
                msg = "🟡 Intraday Update\n"
                for tx_time, tx_type, symbol, qty, price in recent_tx:
                    msg += f"{tx_time.strftime('%H:%M:%S')} — {tx_type} {symbol} {qty:.4f} @ ${price:.6f}\n"
                send_telegram(msg)
        time.sleep(DEX_POLL)

def eod_report_loop():
    while True:
        now = datetime.now()
        if now.hour == EOD_HOUR and now.minute == EOD_MINUTE:
            msg = f"📒 Daily Report ({now.strftime('%Y-%m-%d')})\n"
            msg += format_asset_summary()
            send_telegram(msg)
            intraday_log.clear()
            time.sleep(60)
        time.sleep(30)

# ================== ATH Tracking ==================
ath_prices = {}

def ath_monitor_loop():
    while True:
        for symbol, data in asset_summary.items():
            price = data["price"]
            if symbol not in ath_prices or price > ath_prices[symbol]:
                ath_prices[symbol] = price
                send_telegram(f"🔥 New ATH for {symbol}: ${price:.6f}")
        time.sleep(DEX_POLL)

# ================== Swap Reconciliation ==================
def reconcile_swaps_loop():
    while True:
        # Simple example: check if net qty of any token < 0 (oversell)
        for symbol, data in asset_summary.items():
            net_qty = data["in"] - data["out"]
            if net_qty < 0:
                send_telegram(f"⚠️ Negative net balance detected for {symbol}: {net_qty:.4f}")
        time.sleep(DEX_POLL)

# ================== Main Threading ==================
threads = []

wallet_thread = threading.Thread(target=wallet_monitor_loop, daemon=True)
threads.append(wallet_thread)

discover_thread = threading.Thread(target=discover_pairs_loop, daemon=True)
threads.append(discover_thread)

intraday_thread = threading.Thread(target=intraday_report_loop, daemon=True)
threads.append(intraday_thread)

eod_thread = threading.Thread(target=eod_report_loop, daemon=True)
threads.append(eod_thread)

ath_thread = threading.Thread(target=ath_monitor_loop, daemon=True)
threads.append(ath_thread)

reconcile_thread = threading.Thread(target=reconcile_swaps_loop, daemon=True)
threads.append(reconcile_thread)

if __name__ == "__main__":
    print("Starting full monitor...")
    for t in threads:
        t.start()
    for t in threads:
        t.join()
