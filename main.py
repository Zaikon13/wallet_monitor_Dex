# main.py - Etherscan Multichain (Cronos chainid=25) + Dexscreener monitor
import os
import time
import threading
import requests
from dotenv import load_dotenv

load_dotenv()

# --- Environment variables (must match Railway names exactly) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
ETHERSCAN_API = os.getenv("ETHERSCAN_API")  # Etherscan Multichain API Key (used for Cronos via chainid=25)
DEX_PAIRS = os.getenv("DEX_PAIRS", "")  # comma-separated e.g. "cronos/0x...,cronos/0x..."
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL = int(os.getenv("DEX_POLL", "60"))

# --- Internal state ---
_seen_tx_hashes = set()
_last_prices = {}     # slug -> float
_last_pair_tx = {}    # slug -> last tx hash

# --- Helpers ---
def send_telegram(message):
    """Send message to telegram; if token unauthorized, print clear instruction and exit."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID).")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code == 401:
            print("❌ Telegram API 401 Unauthorized. Regenerate token in @BotFather and update TELEGRAM_BOT_TOKEN in Railway.")
            print("Telegram response:", r.text)
            # fatal: stop process so you won't miss action
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

def safe_json(resp):
    """Return JSON or None. Logs short preview on failure for debugging."""
    if resp is None:
        return None
    if not getattr(resp, "ok", False):
        print("HTTP error fetching:", getattr(resp, "status_code", None), getattr(resp, "text", "")[:300])
        return None
    try:
        return resp.json()
    except Exception:
        preview = (resp.text[:800].replace("\n", " ")) if hasattr(resp, "text") else "<no body>"
        print("Response not JSON (preview):", preview)
        return None

# ---------------- Wallet monitor (Cronos via Etherscan Multichain) ----------------
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"  # V2 multichain endpoint (chainid param required)
CRONOS_CHAINID = 25

def fetch_latest_wallet_txs(limit=25):
    """Fetch latest txs for WALLET_ADDRESS on Cronos via Etherscan Multichain API (chainid=25)."""
    if not WALLET_ADDRESS or not ETHERSCAN_API:
        print("Missing WALLET_ADDRESS or ETHERSCAN_API.")
        return []
    params = {
        "chainid": CRONOS_CHAINID,
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
        r = requests.get(ETHERSCAN_V2_URL, params=params, timeout=15)
        data = safe_json(r)
        if not data:
            return []
        # Etherscan V2 Multichain uses { status, message, result } similarly
        status = str(data.get("status", "")).strip()
        if status == "1" and isinstance(data.get("result"), list):
            return data["result"]
        else:
            # print a short debug message with result preview
            print("Unexpected wallet response:", str(data)[:800])
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
    # send startup message (if telegram configured)
    try:
        send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}`. Watching for new txs.")
    except SystemExit:
        print("Telegram token error on startup. Exiting.")
        return
    while True:
        txs = fetch_latest_wallet_txs(limit=25)
        if not txs:
            print("No txs returned; retrying...")
        else:
            # notify in chronological order (oldest -> newest)
            for tx in reversed(txs):
                if not isinstance(tx, dict):
                    continue
                h = tx.get("hash")
                if not h:
                    continue
                if h not in _seen_tx_hashes:
                    _seen_tx_hashes.add(h)
                    # parse value safely (Cronos 18 decimals)
                    val_raw = tx.get("value", "0")
                    try:
                        value = int(val_raw) / 10**18
                    except Exception:
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

# ---------------- Dexscreener monitor ----------------
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/pairs"

def fetch_pair_data(slug):
    if not slug:
        return None
    url = f"{DEXSCREENER_BASE}/{slug}"
    try:
        r = requests.get(url, timeout=12)
        return safe_json(r)
    except Exception as e:
        print("Error fetching dexscreener", slug, e)
        return None

def dexscreener_loop():
    pairs = [p.strip() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    if not pairs:
        print("No DEX_PAIRS configured; skipping dexscreener monitor.")
        return
    # startup message
    try:
        send_telegram(f"🚀 Dexscreener monitor started for pairs: {', '.join(pairs)}")
    except SystemExit:
        print("Telegram token error on startup (dexscreener). Exiting.")
        return
    for slug in pairs:
        _last_prices[slug] = None
        _last_pair_tx[slug] = None
    while True:
        for slug in pairs:
            data = fetch_pair_data(slug)
            if not data:
                continue
            # normalize structure
            pair = None
            if isinstance(data, dict) and "pair" in data and isinstance(data["pair"], dict):
                pair = data["pair"]
            elif isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list) and data["pairs"]:
                pair = data["pairs"][0]
            else:
                print("Unexpected dexscreener format for", slug)
                continue

            # priceUsd -> float
            price_val = None
            try:
                price_val = float(pair.get("priceUsd") or 0)
            except Exception:
                price_val = None

            prev = _last_prices.get(slug)
            if prev is None and price_val is not None:
                _last_prices[slug] = price_val
                symbol = (pair.get("baseToken") or {}).get("symbol") or slug
                send_telegram(f"📡 Monitoring {symbol} ({slug}) at ${price_val:.6f}")
            elif price_val is not None and prev is not None and prev != 0:
                delta_pct = (price_val - prev) / prev * 100
                if abs(delta_pct) >= PRICE_MOVE_THRESHOLD:
                    symbol = (pair.get("baseToken") or {}).get("symbol") or slug
                    msg = (f"📈 Price move for {symbol} ({slug}): {delta_pct:.2f}%\n"
                           f"Price: ${price_val:.6f} (prev ${prev:.6f})")
                    print("Sending price move alert for", slug, delta_pct)
                    send_telegram(msg)
                _last_prices[slug] = price_val

            # lastTx detection (if present)
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

# ---------------- Entrypoint ----------------
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

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Stopping monitors.")

if __name__ == "__main__":
    main()
