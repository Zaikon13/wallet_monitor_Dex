# main.py (final, production-ready)
import os
import time
import threading
import requests
from dotenv import load_dotenv

load_dotenv()

# --- Environment variables (must match exactly those in Railway) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
ETHERSCAN_API = os.getenv("ETHERSCAN_API")  # Cronoscan API key
DEX_PAIRS = os.getenv("DEX_PAIRS", "")  # comma-separated, e.g. "cronos/0x...,cronos/0x..."
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL = int(os.getenv("DEX_POLL", "60"))

# --- Internal state ---
_seen_tx_hashes = set()
_last_prices = {}      # slug -> last price (float)
_last_pair_tx = {}     # slug -> last tx hash (for trade alerts)

# --- Helpers ---
def send_telegram(message):
    """Send message to Telegram; on 401 stop and log clear instruction."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID).")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code == 401:
            # fatal for notifications — token invalid
            print("❌ Telegram API 401 Unauthorized. Regenerate token in @BotFather and update TELEGRAM_BOT_TOKEN in Railway.")
            print("Telegram response:", r.text)
            raise SystemExit(1)
        if r.status_code != 200:
            print("⚠️ Telegram API returned", r.status_code, r.text)
            return False
        return True
    except SystemExit:
        raise
    except Exception as e:
        print("Exception sending telegram:", e)
        return False

def safe_json(response):
    """Return parsed JSON or None; prints debug if content is not JSON."""
    # If response not OK, print and return None
    if not response.ok:
        print("HTTP error fetching:", response.status_code, response.text[:300])
        return None
    # Try JSON parse
    try:
        return response.json()
    except Exception:
        # Not JSON — print a short preview for debugging
        text = response.text[:600].replace("\n", " ")
        print("Response not JSON (preview):", text)
        return None

# --- Wallet (Cronos) monitor ---
def fetch_latest_wallet_txs(limit=25):
    """Fetch latest transactions from Cronoscan API. Returns list or empty list."""
    if not WALLET_ADDRESS or not ETHERSCAN_API:
        print("Missing WALLET_ADDRESS or ETHERSCAN_API in environment.")
        return []
    url = "https://api.cronoscan.com/api"
    params = {
        "module": "account",
        "action": "txlist",
        "address": WALLET_ADDRESS,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": ETHERSCAN_API,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        data = safe_json(r)
        if not data:
            return []
        # Cronoscan follows Etherscan format: status/message/result
        if str(data.get("status")) == "1" and isinstance(data.get("result"), list):
            return data["result"]
        else:
            # Print full object for debugging (short)
            print("Unexpected wallet response:", str(data)[:600])
            return []
    except Exception as e:
        print("Error fetching wallet txs:", e)
        return []

def wallet_monitor_loop():
    global _seen_tx_hashes
    print("Wallet monitor starting; loading initial recent txs...")
    initial = fetch_latest_wallet_txs(limit=50)
    try:
        _seen_tx_hashes = set(tx.get("hash") for tx in initial if isinstance(tx, dict) and tx.get("hash"))
    except Exception:
        _seen_tx_hashes = set()
    send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}`. Watching for new txs.")
    while True:
        txs = fetch_latest_wallet_txs(limit=25)
        if not txs:
            print("No txs returned; retrying...")
        else:
            # notify in chronological order (oldest first)
            for tx in reversed(txs):
                if not isinstance(tx, dict):
                    continue
                h = tx.get("hash")
                if not h:
                    continue
                if h not in _seen_tx_hashes:
                    _seen_tx_hashes.add(h)
                    # value handling: Cronos uses 18 decimals (wei-like)
                    val_raw = tx.get("value", "0")
                    try:
                        value = int(val_raw) / 10**18
                    except Exception:
                        # safe fallback
                        try:
                            value = float(val_raw)
                        except Exception:
                            value = 0
                    frm = tx.get("from")
                    to = tx.get("to")
                    block = tx.get("blockNumber")
                    tx_link = f"https://cronoscan.com/tx/{h}"
                    msg = (
                        f"*New Cronos TX*\nAddress: `{WALLET_ADDRESS}`\nHash: {tx_link}\n"
                        f"From: `{frm}`\nTo: `{to}`\nValue: {value:.6f} CRO\nBlock: {block}"
                    )
                    print("Sending wallet alert for", h)
                    send_telegram(msg)
        time.sleep(WALLET_POLL)

# --- Dexscreener monitor ---
def fetch_pair_data(slug):
    """Fetch dexscreener pair data for slug like 'cronos/0x...'"""
    url = f"https://api.dexscreener.com/latest/dex/pairs/{slug}"
    try:
        r = requests.get(url, timeout=12)
        data = safe_json(r)
        return data
    except Exception as e:
        print("Error fetching dexscreener", slug, e)
        return None

def dexscreener_loop():
    pairs = [p.strip() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    if not pairs:
        print("No DEX_PAIRS configured; skipping dexscreener monitor.")
        return
    send_telegram(f"🚀 Dexscreener monitor started for pairs: {', '.join(pairs)}")
    # initialize
    for slug in pairs:
        _last_prices[slug] = None
        _last_pair_tx[slug] = None
    while True:
        for slug in pairs:
            data = fetch_pair_data(slug)
            if not data:
                continue
            # support both shapes: { "pair": {...} } or { "pairs": [ {...} ] }
            pair = None
            if isinstance(data, dict) and "pair" in data and isinstance(data["pair"], dict):
                pair = data["pair"]
            elif isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list) and data["pairs"]:
                pair = data["pairs"][0]
            else:
                print("Unexpected dexscreener format for", slug)
                continue

# price
            price_val = None
            try:
                price_val = float(pair.get("priceUsd") or 0)
            except Exception:
                price_val = None

            # detect price movement
            prev = _last_prices.get(slug)
            if prev is None and price_val is not None:
                _last_prices[slug] = price_val
                # initial notice
                symbol = (pair.get("baseToken") or {}).get("symbol") or slug
                send_telegram(f"📡 Monitoring {symbol} ({slug}) at ${price_val:.6f}")
            elif price_val is not None and prev is not None and prev != 0:
                delta_pct = (price_val - prev) / prev * 100
                if abs(delta_pct) >= PRICE_MOVE_THRESHOLD:
                    symbol = (pair.get("baseToken") or {}).get("symbol") or slug
                    msg = (
                        f"📈 Price move for {symbol} ({slug}): {delta_pct:.2f}%\n"
                        f"Price: ${price_val:.6f} (prev ${prev:.6f})"
                    )
                    print("Sending price move alert for", slug, delta_pct)
                    send_telegram(msg)
                _last_prices[slug] = price_val

# detect new lastTx on pair (if available)
            last_tx = pair.get("lastTx") or {}
            last_tx_hash = None
            if isinstance(last_tx, dict):
                last_tx_hash = last_tx.get("hash")
            if last_tx_hash:
                prev_tx = _last_pair_tx.get(slug)
                if prev_tx != last_tx_hash:
                    _last_pair_tx[slug] = last_tx_hash
                    msg = f"🚨 New trade on {slug}\nTx: https://cronoscan.com/tx/{last_tx_hash}"
                    print("Sending trade alert for", slug, last_tx_hash)
                    send_telegram(msg)
        time.sleep(DEX_POLL)

# --- Entrypoint ---
def main():
    print("Starting monitor with config:")
    print("WALLET_ADDRESS:", WALLET_ADDRESS)
    print("TELEGRAM_BOT_TOKEN present:", bool(TELEGRAM_BOT_TOKEN))
    print("TELEGRAM_CHAT_ID:", TELEGRAM_CHAT_ID)
    print("ETHERSCAN_API present:", bool(ETHERSCAN_API))
    print("DEX_PAIRS:", DEX_PAIRS)

 # start threads
    t1 = threading.Thread(target=wallet_monitor_loop, daemon=True)
    t2 = threading.Thread(target=dexscreener_loop, daemon=True)
    t1.start()
    t2.start()

 # keep running
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Stopping monitors.")

if __name__ == "__main__":
    main()
