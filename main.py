# main.py
import os
import time
import threading
import requests
from dotenv import load_dotenv

load_dotenv()

# Environment (defaults set to the values you already provided earlier)
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8392560160:AAEwnLwglovpCiJ2y2ypcXdiBWrtEQ1QLrs")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5307877340")
WALLET = os.getenv("WALLET_ADDRESS", "0xEa53D79ce2A915033e6b4C5ebE82bb6b292E35Cc")
ETHERSCAN_API = os.getenv("ETHERSCAN_API", "6UIEEA68VIX24SJ5JSCX7YN5W72D6WVNPH")
# Dexscreener pairs: comma-separated slugs (example slugs from Cronos network)
DEX_PAIRS = os.getenv("DEX_PAIRS", "cronos/0x08924f0c6eb6efc0be1819b7355829d6e7714f4b,cronos/0x308212a0c6d2a4a99ac34f72727e0d3e3f08b891")
# Optional: dexscreener has no required key for public endpoints; keep placeholder
DEX_API_KEY = os.getenv("DEX_API_KEY", "")

# Poll intervals (secs)
WALLET_POLL = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL = int(os.getenv("DEX_POLL", "60"))
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))  # percent

# State
_seen_tx_hashes = set()
_last_prices = {}

# Helpers
def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram not configured; cannot send message.")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code != 200:
            print("Telegram API error:", r.status_code, r.text)
    except Exception as e:
        print("Exception sending telegram:", e)

def fetch_latest_wallet_txs():
    # Use Cronoscan API (Etherscan-style)
    try:
        url = ("https://api.cronoscan.com/api"
               f"?module=account&action=txlist&address={WALLET}"
               f"&startblock=0&endblock=99999999&page=1&offset=10&sort=desc&apikey={ETHERSCAN_API}")
        r = requests.get(url, timeout=15)
        data = r.json()
        # data expected: {"status":"1","message":"OK","result":[...]}
        result = data.get("result", [])
        if isinstance(result, list):
            return result
        else:
            print("Unexpected wallet result format:", type(result))
            return []
    except Exception as e:
        print("Error fetching wallet txs:", e)
        return []

def wallet_monitor_loop():
    global _seen_tx_hashes
    print("Wallet monitor starting; loading initial recent txs...")
    txs = fetch_latest_wallet_txs()
    # Initialize seen set so we don't flood with old txs on start
    try:
        _seen_tx_hashes = set(tx["hash"] for tx in txs if isinstance(tx, dict) and tx.get("hash"))
    except Exception:
        _seen_tx_hashes = set()
    send_telegram(f"🚀 Wallet monitor started for `{WALLET}`. Watching for new txs.")
    while True:
        txs = fetch_latest_wallet_txs()
        # txs[0] is newest (desc)
        if not txs:
            print("No txs returned; retrying...")
        else:
            # iterate from oldest->newest to notify in chronological order
            for tx in reversed(txs):
                if not isinstance(tx, dict):
                    continue
                h = tx.get("hash")
                if not h:
                    continue
                if h not in _seen_tx_hashes:
                    _seen_tx_hashes.add(h)
                    # build message
                    value = int(tx.get("value", "0")) / 10**18 if tx.get("value") and tx.get("value").isdigit() else 0
                    frm = tx.get("from")
                    to = tx.get("to")
                    block = tx.get("blockNumber")
                    etherscan_link = f"https://cronoscan.com/tx/{h}"
                    msg = (f"*New Cronos TX*\nAddress: `{WALLET}`\nHash: {etherscan_link}\nFrom: `{frm}`\nTo: `{to}`\n"
                           f"Value: {value:.6f} CRO\nBlock: {block}")
                    print("Sending wallet alert for", h)
                    send_telegram(msg)
        time.sleep(WALLET_POLL)

# Dexscreener monitor
def fetch_pair_data(slug):
    try:
        url = f"https://api.dexscreener.com/latest/dex/pairs/{slug}"
        r = requests.get(url, timeout=15)
        return r.json()
    except Exception as e:
        print("Error fetching dexscreener", slug, e)
        return None

def dexscreener_loop():
    pairs = [p.strip() for p in DEX_PAIRS.split(",") if p.strip()]
    if not pairs:
        print("No DEX pairs configured.")
        return
    send_telegram(f"🚀 Dexscreener monitor started for pairs: {', '.join(pairs)}")
    for slug in pairs:
        _last_prices[slug] = None
    while True:
        for slug in pairs:
            data = fetch_pair_data(slug)
            if not data:
                continue
            pair = data.get("pair") or {}
            # priceUsd might be string
            price_str = pair.get("priceUsd")
            try:
                price = float(price_str) if price_str is not None else None
            except Exception:
                price = None
            # lastTx detection (if available)
            last_tx = pair.get("lastTx") or {}
            last_tx_hash = last_tx.get("hash") if isinstance(last_tx, dict) else None
            # volume 1h
            vol_h1 = None
            vol = pair.get("volume", {})
            if isinstance(vol, dict):
                try:
                    vol_h1 = float(vol.get("h1", 0))
                except Exception:
                    vol_h1 = None
            # price change reported (if any)
            price_change_h1 = None
            pc = pair.get("priceChange", {})
            if isinstance(pc, dict):
                try:
                    price_change_h1 = float(pc.get("h1") or 0)
                except Exception:
                    price_change_h1 = None
            # detect price movement relative to last seen
            prev = _last_prices.get(slug)
            if prev is None and price is not None:
                _last_prices[slug] = price
            elif price is not None and prev is not None:
                delta_pct = (price - prev) / prev * 100 if prev != 0 else 0
                if abs(delta_pct) >= PRICE_MOVE_THRESHOLD:
                    msg = (f"📈 {slug} moved {delta_pct:.2f}% (threshold {PRICE_MOVE_THRESHOLD}%)\n"
                           f"Price: ${price:.6f}\nVol1h: {vol_h1}\nChange(h1): {price_change_h1}")
                    print("Sending price movement alert for", slug, delta_pct)
                    send_telegram(msg)
                _last_prices[slug] = price
            # detect new lastTx
            key = f"{slug}_lasttx"
            if last_tx_hash:
                prev_tx = _last_prices.get(key)
                if prev_tx != last_tx_hash:
                    # new tx on pair
                    _last_prices[key] = last_tx_hash
                    msg = f"🚨 New trade on {slug}\nTx: https://cronoscan.com/tx/{last_tx_hash}"
                    print("Sending trade alert for", slug, last_tx_hash)
                    send_telegram(msg)
        time.sleep(DEX_POLL)

def main():
    # sanity checks printed at startup
    print("Starting monitor with config:")
    print("WALLET:", WALLET)
    print("BOT_TOKEN present:", bool(BOT_TOKEN))
    print("CHAT_ID:", CHAT_ID)
    print("ETHERSCAN_API present:", bool(ETHERSCAN_API))
    print("DEX_PAIRS:", DEX_PAIRS)
    # start threads
    t1 = threading.Thread(target=wallet_monitor_loop, daemon=True)
    t2 = threading.Thread(target=dexscreener_loop, daemon=True)
    t1.start()
    t2.start()
    # keep main alive
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()
