# main.py - Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner + Auto Discovery
# Plug-and-play. Uses EXACT Railway env var names you already set.

import os
import time
import threading
from collections import deque
import requests
from dotenv import load_dotenv

load_dotenv()

# ========================= Environment (exact names) =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS     = os.getenv("WALLET_ADDRESS")
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")       # Etherscan Multichain (used with chainid=25)
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")       # "cronos/0xPAIR1,cronos/0xPAIR2"
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL           = int(os.getenv("DEX_POLL", "60"))

# ====== Optional (defaults ok even if you don't set them in Railway) ======
PRICE_WINDOW       = int(os.getenv("PRICE_WINDOW", "3"))          # samples kept for spike detection
SPIKE_THRESHOLD    = float(os.getenv("SPIKE_THRESHOLD", "8.0"))   # % change across PRICE_WINDOW
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

DISCOVER_ENABLED   = os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on")
DISCOVER_QUERY     = os.getenv("DISCOVER_QUERY", "cronos")        # search keyword; we filter to cronos anyway
DISCOVER_LIMIT     = int(os.getenv("DISCOVER_LIMIT", "10"))       # max new pairs to adopt per discovery round
DISCOVER_POLL      = int(os.getenv("DISCOVER_POLL", "120"))       # seconds
TOKENS             = os.getenv("TOKENS", "")                      # e.g. "cronos/0xTokenA,cronos/0xTokenB"

# ========================= Constants =========================
ETHERSCAN_V2_URL   = "https://api.etherscan.io/v2/api"            # multichain endpoint (needs chainid)
CRONOS_CHAINID     = 25
DEX_BASE_PAIRS     = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS    = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH    = "https://api.dexscreener.com/latest/dex/search"
DEXSITE_PAIR       = "https://dexscreener.com/{chain}/{pair}"     # for user links
CRONOS_TX          = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL       = "https://api.telegram.org/bot{token}/sendMessage"

# Global session with UA to avoid 403s
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
})

# ========================= State =========================
_seen_tx_hashes   = set()
_last_prices      = {}                    # slug -> float
_price_history    = {}                    # slug -> deque
_last_pair_tx     = {}                    # slug -> tx hash
_rate_limit_last  = 0.0                   # simple TG rate limit
_tracked_pairs    = set()                 # set of slugs "cronos/0xPAIR"
_known_pairs_meta = {}                    # slug -> dict(meta we got from API), optional

# ========================= Utils =========================
def send_telegram(message: str) -> bool:
    global _rate_limit_last
    now = time.time()
    # soft rate limit 0.8s
    if now - _rate_limit_last < 0.8:
        time.sleep(0.8 - (now - _rate_limit_last))

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID).")
        return False

    url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = SESSION.post(url, data=payload, timeout=12)
        _rate_limit_last = time.time()
        if r.status_code == 401:
            print("❌ Telegram 401 Unauthorized. Regenerate token in @BotFather and update TELEGRAM_BOT_TOKEN.")
            print("Telegram response:", r.text[:300])
            raise SystemExit(1)
        if r.status_code != 200:
            print("⚠️ Telegram API returned", r.status_code, r.text[:300])
            return False
        return True
    except SystemExit:
        raise
    except Exception as e:
        print("Exception sending telegram:", e)
        return False

def safe_json(r):
    if r is None:
        return None
    if not getattr(r, "ok", False):
        print("HTTP error:", getattr(r, "status_code", None), str(getattr(r, "text", ""))[:300])
        return None
    try:
        return r.json()
    except Exception:
        txt = (r.text[:800].replace("\n", " ")) if hasattr(r, "text") else "<no body>"
        print("Response not JSON (preview):", txt)
        return None

# ========================= Wallet (Cronos via Etherscan Multichain) =========================
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
        r = SESSION.get(ETHERSCAN_V2_URL, params=params, timeout=15)
        data = safe_json(r)
        if not data:
            return []
        if str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
            return data["result"]
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

    try:
        send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}` (Cronos).")
    except SystemExit:
        print("Telegram token error on startup. Exiting wallet monitor.")
        return

    while True:
        txs = fetch_latest_wallet_txs(limit=25)
        if not txs:
            print("No txs returned; retrying...")
        else:
            for tx in reversed(txs):  # send oldest→newest
                if not isinstance(tx, dict):
                    continue
                h = tx.get("hash")
                if not h or h in _seen_tx_hashes:
                    continue
                _seen_tx_hashes.add(h)

                # value (Cronos native 18 decimals)
                val_raw = tx.get("value", "0")
                try:
                    value = int(val_raw) / 10**18
                except Exception:
                    try:
                        value = float(val_raw)
                    except Exception:
                        value = 0.0

                frm  = tx.get("from")
                to   = tx.get("to")
                blk  = tx.get("blockNumber")
                link = CRONOS_TX.format(txhash=h)

                msg = (
                    f"*New Cronos TX*\n"
                    f"Address: `{WALLET_ADDRESS}`\n"
                    f"Hash: {link}\n"
                    f"From: `{frm}`\n"
                    f"To: `{to}`\n"
                    f"Value: {value:.6f} CRO\n"
                    f"Block: {blk}"
                )
                print("Sending wallet alert for", h)
                send_telegram(msg)
        time.sleep(WALLET_POLL)

# ========================= Dexscreener helpers =========================
def slug(chain: str, pair_address: str) -> str:
    return f"{chain}/{pair_address}".lower()

def fetch_pair(slug_str: str):
    # slug is "cronos/0xPAIR"
    url = f"{DEX_BASE_PAIRS}/{slug_str}"
    try:
        r = SESSION.get(url, timeout=12)
        return safe_json(r)
    except Exception as e:
        print("Error fetching pair", slug_str, e)
        return None

def fetch_token_pairs(chain: str, token_address: str):
    # returns pairs list for a token (we'll pick the first/top)
    url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
    try:
        r = SESSION.get(url, timeout=12)
        data = safe_json(r)
        if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
            return data["pairs"]
        return []
    except Exception as e:
        print("Error fetching token", chain, token_address, e)
        return []

def fetch_search(query: str):
    # generic search (we’ll filter to chainId == 'cronos')
    try:
        r = SESSION.get(DEX_BASE_SEARCH, params={"q": query}, timeout=15)
        data = safe_json(r)
        if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
            return data["pairs"]
        return []
    except Exception as e:
        print("Error search dexscreener:", e)
        return []

def ensure_tracking_pair(chain: str, pair_address: str, meta: dict = None):
    s = slug(chain, pair_address)
    if s not in _tracked_pairs:
        _tracked_pairs.add(s)
        _last_prices[s]   = None
        _last_pair_tx[s]  = None
        _price_history[s] = deque(maxlen=PRICE_WINDOW)
        if meta:
            _known_pairs_meta[s] = meta
        # notify adoption
        ds_link = DEXSITE_PAIR.format(chain=chain, pair=pair_address)
        sym = None
        if isinstance(meta, dict):
            bt = meta.get("baseToken") or {}
            sym = bt.get("symbol")
        title = f"{sym} ({s})" if sym else s
        send_telegram(f"🆕 Now monitoring pair: {title}\n{ds_link}")

# ========================= Dexscreener monitor (pairs already tracked) =========================
def update_price_history(slg, price):
    hist = _price_history.get(slg)
    if hist is None:
        hist = deque(maxlen=PRICE_WINDOW)
        _price_history[slg] = hist
    hist.append(price)
    _last_prices[slg] = price

def detect_spike(slg):
    hist = _price_history.get(slg)
    if not hist or len(hist) < 2:
        return None
    first = hist[0]
    last  = hist[-1]
    if first in (None, 0):
        return None
    pct = (last - first) / first * 100.0
    return pct if abs(pct) >= SPIKE_THRESHOLD else None

def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        print("No tracked pairs; monitor waits until discovery/seed adds some.")
        # still run loop to allow discovery thread to fill
    else:
        send_telegram(f"🚀 Dexscreener monitor started for: {', '.join(sorted(_tracked_pairs))}")

    while True:
        if not _tracked_pairs:
            time.sleep(DEX_POLL)
            continue

        for s in list(_tracked_pairs):
            data = fetch_pair(s)
            if not data:
                continue

            # normalize structure
            pair = None
            if isinstance(data, dict) and isinstance(data.get("pair"), dict):
                pair = data["pair"]
            elif isinstance(data, dict) and isinstance(data.get("pairs"), list) and data["pairs"]:
                pair = data["pairs"][0]
            else:
                print("Unexpected dexscreener format for", s)
                continue

            # price
            price_val = None
            try:
                price_val = float(pair.get("priceUsd") or 0)
            except Exception:
                price_val = None

            # vol (h1, optional)
            vol_h1 = None
            vol = pair.get("volume") or {}
            if isinstance(vol, dict):
                try:
                    vol_h1 = float(vol.get("h1") or 0)
                except Exception:
                    vol_h1 = None

            # symbol
            symbol = (pair.get("baseToken") or {}).get("symbol") or s

            # history + spike
            if price_val is not None:
                update_price_history(s, price_val)
                spike_pct = detect_spike(s)
                if spike_pct is not None:
                    if MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 < MIN_VOLUME_FOR_ALERT:
                        pass
                    else:
                        send_telegram(
                            f"🚨 Spike on {symbol}: {spike_pct:.2f}% over last {len(_price_history[s])} samples\n"
                            f"Price: ${price_val:.6f} Vol1h: {vol_h1}"
                        )
                        _price_history[s].clear()
                        _last_prices[s] = price_val

            # price move vs last
            prev = _last_prices.get(s)
            if prev is not None and price_val is not None and prev != 0:
                delta = (price_val - prev) / prev * 100.0
                if abs(delta) >= PRICE_MOVE_THRESHOLD:
                    send_telegram(
                        f"📈 Price move on {symbol}: {delta:.2f}%\n"
                        f"Price: ${price_val:.6f} (prev ${prev:.6f})"
                    )
                    _last_prices[s] = price_val

            # lastTx detection
            last_tx = pair.get("lastTx") or {}
            last_tx_hash = last_tx.get("hash") if isinstance(last_tx, dict) else None
            if last_tx_hash:
                prev_tx = _last_pair_tx.get(s)
                if prev_tx != last_tx_hash:
                    _last_pair_tx[s] = last_tx_hash
                    send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx_hash)}")

        time.sleep(DEX_POLL)

# ========================= Auto discovery (search + tokens) =========================
def discovery_loop():
    # 1) Seed from fixed DEX_PAIRS (existing env)
    seeds = [p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    for s in seeds:
        # accept only cronos/* slugs, ignore others quietly
        if s.startswith("cronos/"):
            ensure_tracking_pair("cronos", s.split("/",1)[1])

    # 2) Seed from TOKENS (optional): resolve each token to its top pair and track it
    token_items = [t.strip().lower() for t in (TOKENS or "").split(",") if t.strip()]
    for t in token_items:
        if not t.startswith("cronos/"):
            continue
        _, token_addr = t.split("/", 1)
        pairs = fetch_token_pairs("cronos", token_addr)
        if pairs:
            # pick the first (dexscreener usually orders by liquidity/relevance)
            p = pairs[0]
            pair_addr = p.get("pairAddress")
            if pair_addr:
                ensure_tracking_pair("cronos", pair_addr, meta=p)

    # 3) Continuous discovery from /search
    if not DISCOVER_ENABLED:
        print("Discovery disabled (DISCOVER_ENABLED=false).")
        return

    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos).")

    while True:
        try:
            found = fetch_search(DISCOVER_QUERY)
            adopted = 0
            for p in found or []:
                # keep only Cronos
                if str(p.get("chainId", "")).lower() != "cronos":
                    continue
                pair_addr = p.get("pairAddress")
                if not pair_addr:
                    continue
                s = slug("cronos", pair_addr)
                if s in _tracked_pairs:
                    continue
                # adopt new pair
                ensure_tracking_pair("cronos", pair_addr, meta=p)
                adopted += 1
                if adopted >= DISCOVER_LIMIT:
                    break
        except Exception as e:
            print("Discovery error:", e)

        time.sleep(DISCOVER_POLL)

# ========================= Entrypoint =========================
def main():
    print("Starting monitor with config:")
    print("WALLET_ADDRESS:", WALLET_ADDRESS)
    print("TELEGRAM_BOT_TOKEN present:", bool(TELEGRAM_BOT_TOKEN))
    print("TELEGRAM_CHAT_ID:", TELEGRAM_CHAT_ID)
    print("ETHERSCAN_API present:", bool(ETHERSCAN_API))
    print("DEX_PAIRS:", DEX_PAIRS)
    print("DISCOVER_ENABLED:", DISCOVER_ENABLED, "| DISCOVER_QUERY:", DISCOVER_QUERY)

    # Threads
    t_wallet  = threading.Thread(target=wallet_monitor_loop, daemon=True)
    t_pairs   = threading.Thread(target=monitor_tracked_pairs_loop, daemon=True)
    t_discover= threading.Thread(target=discovery_loop, daemon=True)

    t_wallet.start()
    t_pairs.start()
    t_discover.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Stopping monitors.")

if __name__ == "__main__":
    main()
