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

# ========================= 2/2 main.py =========================

# Aggregation & PnL calculation
def update_aggregates(tx):
    asset = tx['token']
    amount = tx['amount']
    usd_value = tx['usd_value']
    if asset not in aggregates:
        aggregates[asset] = {'qty': 0.0, 'realized': 0.0, 'unrealized': 0.0, 'last_price': 0.0}
    if tx['type'] == 'IN':
        aggregates[asset]['qty'] += amount
        aggregates[asset]['last_price'] = tx['price']
    elif tx['type'] == 'OUT':
        sold_qty = min(amount, aggregates[asset]['qty'])
        realized = sold_qty * tx['price']
        aggregates[asset]['qty'] -= sold_qty
        aggregates[asset]['realized'] += realized

# Intraday & EOD reporting
def send_report(eod=False):
    report_type = "EOD" if eod else "Intraday"
    message = f"🟡 {report_type} Update\n📒 Report ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})\nTransactions:\n"
    for tx in recent_txs:
        message += f"• {tx['time']} — {tx['type']} {tx['token']} {tx['amount']} @ ${tx['price']} (${'{:.4f}'.format(tx['usd_value'])})\n"
    message += "\nPer-Asset Summary:\n"
    for asset, data in aggregates.items():
        message += f"• {asset}: qty {data['qty']} | realized ${'{:.4f}'.format(data['realized'])} | last price ${data['last_price']}\n"
    send_telegram(message)

# ATH & spike alerts
def check_spikes(token, price):
    last_ath = ath_prices.get(token, 0)
    if price > last_ath:
        ath_prices[token] = price
        send_telegram(f"🚀 New ATH for {token}: ${price}")
    elif (price - last_ath) / last_ath * 100 >= SPIKE_THRESHOLD:
        send_telegram(f"⚡ Price spike detected for {token}: ${price}")

# Discovery thread
def discovery_loop():
    while True:
        if DISCOVER_ENABLED:
            discover_pairs()
        time.sleep(DISCOVER_POLL)

# Wallet monitor thread
def wallet_loop():
    while True:
        txs = fetch_wallet_txs(WALLET_ADDRESS)
        for tx in txs:
            if tx not in recent_txs_set:
                recent_txs.append(tx)
                recent_txs_set.add(tx)
                update_aggregates(tx)
                if tx['token'] in tracked_pairs:
                    check_spikes(tx['token'], tx['price'])
        time.sleep(WALLET_POLL)

# Dex pairs monitor thread
def pairs_loop():
    while True:
        scan_pairs()
        time.sleep(DEX_POLL)

# Scheduler
def start_scheduler():
    threading.Thread(target=wallet_loop, daemon=True).start()
    threading.Thread(target=pairs_loop, daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()
    threading.Thread(target=intraday_scheduler, daemon=True).start()
    threading.Thread(target=eod_scheduler, daemon=True).start()

if __name__ == "__main__":
    aggregates = {}
    ath_prices = {}
    recent_txs = deque(maxlen=500)
    recent_txs_set = set()
    tracked_pairs = set()
    start_scheduler()
    while True:
        time.sleep(1)
