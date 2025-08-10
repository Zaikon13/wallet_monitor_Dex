# main.py - Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner
# Plug-and-play. Uses the exact Railway env var names you already have.
import os
import time
import threading
import requests
from collections import deque
from dotenv import load_dotenv

load_dotenv()

# ========== Environment variables (MUST match Railway names EXACTLY) ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
ETHERSCAN_API = os.getenv("ETHERSCAN_API")  # Etherscan Multichain key (used with chainid=25)
DEX_PAIRS = os.getenv("DEX_PAIRS", "")      # comma-separated slugs like "cronos/0x...,cronos/0x..."
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL = int(os.getenv("DEX_POLL", "30"))

# Additional tuning (you can set these env vars if you want)
PRICE_WINDOW = int(os.getenv("PRICE_WINDOW", "3"))     # how many last prices to keep to detect spikes (small integer)
SPIKE_THRESHOLD = float(os.getenv("SPIKE_THRESHOLD", "8.0"))  # percent change within PRICE_WINDOW to call "spike"
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))  # optional min vol threshold

# ========== Internal state ==========
_seen_tx_hashes = set()
_last_prices = {}         # slug -> last observed price (float)
_price_history = {}       # slug -> deque([p1, p2, ...]) length PRICE_WINDOW
_last_pair_tx = {}        # slug -> last tx hash (to dedupe trade alerts)
_token_balances = {}      # token_contract -> last balance (for wallet token monitoring)
_rate_limit_last_sent = 0 # global simple rate limiter timestamp

# Constants
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID = 25
DEXSCREENER_BASE = "https://api.dexscreener.com/latest/dex/pairs"
TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

# ========== Helpers ==========
def send_telegram(message):
    """Send message to Telegram. On 401 exit so you can fix token."""
    global _rate_limit_last_sent
    # Simple rate limit: at most one message per 0.8s to avoid flooding
    now = time.time()
    if now - _rate_limit_last_sent < 0.8:
        time.sleep(0.8 - (now - _rate_limit_last_sent))
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID).")
        return False
    url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=10)
        _rate_limit_last_sent = time.time()
        if r.status_code == 401:
            print("❌ Telegram API 401 Unauthorized. Regenerate token in @BotFather and update TELEGRAM_BOT_TOKEN in Railway.")
            print("Telegram response:", r.text)
            raise SystemExit(1)
        if r.status_code != 200:
            print("⚠️ Telegram API returned", r.status_code, r.text[:400])
            return False
        return True
    except SystemExit:
        raise
    except Exception as e:
        print("Exception sending telegram:", e)
        return False

def safe_json(resp):
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

# ========== Wallet (Cronos via Etherscan Multichain) ==========
def fetch_latest_wallet_txs(limit=25):
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
        status = str(data.get("status", "")).strip()
        if status == "1" and isinstance(data.get("result"), list):
            return data["result"]
        else:
            # Print a short preview
            print("Unexpected wallet response:", str(data)[:600])
            return []
    except Exception as e:
        print("Error fetching wallet txs:", e)
        return []

def fetch_token_balances_from_txlist(txs):
    """
    Extract token transfers from txlist results (ERC20 tokenTx not always present in txlist),
    so we better query token transfers separately if needed. Here: keep it simple:
    - we inspect txs for value transfers in CRO (native) and for token transfer logs (if returned as logs)
    """
    # For robust token balances you would call the tokenbalance endpoint per token contract.
    # Here we provide a lightweight approach: gather contract addresses from token transfers if present in tx results.
    tokens = {}
    for tx in txs:
        # many txs from txlist are native transfers; token transfers require separate endpoint `tokentx`
        # we leave heavy token balance fetching optional (not enabled by default)
        pass
    return tokens

def wallet_monitor_loop():
    global _seen_tx_hashes, _token_balances
    print("Wallet monitor starting; loading initial recent txs...")
    initial = fetch_latest_wallet_txs(limit=50)
    try:
        _seen_tx_hashes = set(tx.get("hash") for tx in initial if isinstance(tx, dict) and tx.get("hash"))
    except Exception:
        _seen_tx_hashes = set()
    # Inform startup
    try:
        send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}`. Watching for new txs.")
    except SystemExit:
        print("Telegram token error on startup. Exiting wallet monitor.")
        return

    while True:
        txs = fetch_latest_wallet_txs(limit=25)
        if not txs:
            print("No txs returned; retrying...")
        else:
            for tx in reversed(txs):  # oldest → newest
                if not isinstance(tx, dict):
                    continue
                h = tx.get("hash")
                if not h:
                    continue
                if h not in _seen_tx_hashes:
                    _seen_tx_hashes.add(h)
                    # value handling
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
                    blk = tx.get("blockNumber")
                    link = f"https://cronoscan.com/tx/{h}"
                    msg = (
                        f"*New Cronos TX*\nAddress: `{WALLET_ADDRESS}`\nHash: {link}\n"
                        f"From: `{frm}`\nTo: `{to}`\nValue: {value:.6f} CRO\nBlock: {blk}"
                    )
                    print("Sending wallet alert for", h)
                    send_telegram(msg)
        time.sleep(WALLET_POLL)

# ========== Dexscreener Live Scanner ==========
def fetch_pair_raw(slug):
    if not slug:
        return None
    url = f"{DEXSCREENER_BASE}/{slug}"
    try:
        r = requests.get(url, timeout=12)
        return safe_json(r)
    except Exception as e:
        print("Error fetching dexscreener", slug, e)
        return None

def update_price_history(slug, price):
    hist = _price_history.get(slug)
    if hist is None:
        hist = deque(maxlen=PRICE_WINDOW)
        _price_history[slug] = hist
    hist.append(price)
    _last_prices[slug] = price

def detect_spike(slug):
    hist = _price_history.get(slug)
    if not hist or len(hist) < 2:
        return None
    first = hist[0]
    last = hist[-1]
    if first == 0 or first is None:
        return None
    pct = (last - first) / first * 100
    if abs(pct) >= SPIKE_THRESHOLD:
        return pct
    return None

def dexscreener_loop():
    pairs = [p.strip() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    if not pairs:
        print("No DEX_PAIRS configured; skipping dexscreener monitor.")
        return
    # startup
    try:
        send_telegram(f"🚀 Dexscreener monitor started for pairs: {', '.join(pairs)}")
    except SystemExit:
        print("Telegram token error on startup (dex). Exiting dexscreener.")
        return

    # init arrays
    for slug in pairs:
        _last_prices[slug] = None
        _last_pair_tx[slug] = None
        _price_history[slug] = deque(maxlen=PRICE_WINDOW)

    while True:
        for slug in pairs:
            raw = fetch_pair_raw(slug)
            if not raw:
                continue
            # normalise possible shapes
            pair = None
            if isinstance(raw, dict) and "pair" in raw and isinstance(raw["pair"], dict):
                pair = raw["pair"]
            elif isinstance(raw, dict) and "pairs" in raw and isinstance(raw["pairs"], list) and raw["pairs"]:
                pair = raw["pairs"][0]
            else:
                # if Dexscreener returns unexpected, log and skip
                print("Unexpected dexscreener format for", slug)
                continue

            # price
            price_val = None
            try:
                price_val = float(pair.get("priceUsd") or 0)
            except Exception:
                price_val = None

            # volume (h1) optional
            vol_h1 = None
            vol = pair.get("volume") or {}
            if isinstance(vol, dict):
                try:
                    vol_h1 = float(vol.get("h1") or 0)
                except Exception:
                    vol_h1 = None

            # update history and detect spike
            if price_val is not None:
                update_price_history(slug, price_val)
                spike_pct = detect_spike(slug)
                # only alert on significant spike and optional volume threshold
                if spike_pct is not None:
                    if MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 < MIN_VOLUME_FOR_ALERT:
                        # skip small-volume spikes
                        pass
                    else:
                        symbol = (pair.get("baseToken") or {}).get("symbol") or slug
                        msg = (f"🚨 Spike detected for {symbol} ({slug}): {spike_pct:.2f}% over last {len(_price_history[slug])} samples\n"
                               f"Price: ${price_val:.6f} Vol1h: {vol_h1}")
                        # dedupe using last prices: only send if last spike different
                        print("Sending spike alert for", slug, spike_pct)
                        send_telegram(msg)
                        # reset history after spike to avoid repeated alerts
                        _price_history[slug].clear()
                        _last_prices[slug] = price_val

            # detect price move beyond PRICE_MOVE_THRESHOLD relative to last price
            prev = _last_prices.get(slug)
            if prev is not None and price_val is not None and prev != 0:
                delta_pct = (price_val - prev) / prev * 100
                if abs(delta_pct) >= PRICE_MOVE_THRESHOLD:
                    symbol = (pair.get("baseToken") or {}).get("symbol") or slug
                    msg = (f"📈 Price move for {symbol} ({slug}): {delta_pct:.2f}%\n"
                           f"Price: ${price_val:.6f} (prev ${prev:.6f})")
                    print("Sending price move alert for", slug, delta_pct)
                    send_telegram(msg)
                    _last_prices[slug] = price_val

            # detect new lastTx (trade)
            last_tx = pair.get("lastTx") or {}
            last_tx_hash = None
            if isinstance(last_tx, dict):
                last_tx_hash = last_tx.get("hash")
            if last_tx_hash:
                prev_tx = _last_pair_tx.get(slug)
                if prev_tx != last_tx_hash:
                    _last_pair_tx[slug] = last_tx_hash
                    msg = f"🔔 New trade on {slug}\nTx: https://cronoscan.com/tx/{last_tx_hash}"
                    print("Sending trade alert for", slug, last_tx_hash)
                    send_telegram(msg)
        time.sleep(DEX_POLL)

# ========== Entrypoint ==========
def main():
    print("Starting monitor with config:")
    print("WALLET_ADDRESS:", WALLET_ADDRESS)
    print("TELEGRAM_BOT_TOKEN present:", bool(TELEGRAM_BOT_TOKEN))
    print("TELEGRAM_CHAT_ID:", TELEGRAM_CHAT_ID)
    print("ETHERSCAN_API present:", bool(ETHERSCAN_API))
    print("DEX_PAIRS:", DEX_PAIRS)

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
