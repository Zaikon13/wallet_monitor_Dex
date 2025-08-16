# main.py - Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner + Auto Discovery + PnL reports
# Plug-and-play. Uses EXACT Railway env var names you already set.

import os
import time
import threading
from collections import deque, defaultdict
import json
import math
from datetime import datetime, timedelta
import requests
from dotenv import load_dotenv

load_dotenv()

# ========================= Environment (exact names) =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")       # Etherscan Multichain (used with chainid=25)
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")       # "cronos/0xPAIR1,cronos/0xPAIR2"
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL           = int(os.getenv("DEX_POLL", "60"))

# ====== Optional (defaults ok even if you don't set them in Railway) ======
PRICE_WINDOW          = int(os.getenv("PRICE_WINDOW", "3"))          # samples kept for spike detection
SPIKE_THRESHOLD       = float(os.getenv("SPIKE_THRESHOLD", "8.0"))   # % change across PRICE_WINDOW
MIN_VOLUME_FOR_ALERT  = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

DISCOVER_ENABLED      = os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on")
DISCOVER_QUERY        = os.getenv("DISCOVER_QUERY", "cronos")        # search keyword; we filter to cronos anyway
DISCOVER_LIMIT        = int(os.getenv("DISCOVER_LIMIT", "10"))       # max new pairs to adopt per discovery round
DISCOVER_POLL         = int(os.getenv("DISCOVER_POLL", "120"))       # seconds
TOKENS                = os.getenv("TOKENS", "")                      # e.g. "cronos/0xTokenA,cronos/0xTokenB"

# ====== Reporting schedule (optional) ======
TZ                    = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS        = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR              = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE            = int(os.getenv("EOD_MINUTE", "59"))

# ========================= Constants =========================
ETHERSCAN_V2_URL   = "https://api.etherscan.io/v2/api"            # multichain endpoint (needs chainid)
CRONOS_CHAINID     = 25
DEX_BASE_PAIRS     = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS    = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH    = "https://api.dexscreener.com/latest/dex/search"
DEXSITE_PAIR       = "https://dexscreener.com/{chain}/{pair}"     # for user links
CRONOS_TX          = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL       = "https://api.telegram.org/bot{token}/sendMessage"

DATA_DIR           = "/app/data"

# Apply timezone (best effort)
try:
    os.environ["TZ"] = TZ
    if hasattr(time, "tzset"):
        time.tzset()
except Exception:
    pass

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

# --- PnL / ledger state ---
_day_ledger_lock  = threading.Lock()
_token_balances   = defaultdict(float)    # token_address (or "CRO") -> amount
_token_meta       = {}                    # token_address -> {"symbol":..., "decimals":...}
# Note: For native CRO we use key "CRO"
_last_intraday_sent = 0.0

# Ensure data dir
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

# ========================= Utils =========================
def now_dt():
    return datetime.now()

def ymd(dt=None):
    if dt is None:
        dt = now_dt()
    return dt.strftime("%Y-%m-%d")

def month_prefix(dt=None):
    if dt is None:
        dt = now_dt()
    return dt.strftime("%Y-%m")

def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

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

# cache για τιμές (TTL σε δευτ.)
PRICE_CACHE = {}
PRICE_CACHE_TTL = 60

# ---------- Improved price helpers (Dexscreener + CoinGecko fallbacks) ----------
def _top_price_from_pairs(pairs):
    if not pairs:
        return None
    best = None
    best_liq = -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")).lower() != "cronos" and p.get("chainId") != "cronos":
                # some results use numeric chainId; we keep only cronos textual tag if present
                pass
            # liquidity usd (may be missing)
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq = liq
                best = price
        except Exception:
            continue
    return best

def _price_from_dexscreener_token(token_addr):
    # use /tokens/cronos/{token} endpoint to get top pairs -> priceUsd
    try:
        url = f"{DEX_BASE_TOKENS}/cronos/{token_addr}"
        r = SESSION.get(url, timeout=12)
        data = safe_json(r)
        if not data:
            return None
        pairs = data.get("pairs") if isinstance(data, dict) else None
        return _top_price_from_pairs(pairs)
    except Exception as e:
        print("Error _price_from_dexscreener_token:", e)
        return None

def _price_from_dexscreener_search(symbol_or_query):
    try:
        r = SESSION.get(DEX_BASE_SEARCH, params={"q": symbol_or_query}, timeout=12)
        data = safe_json(r)
        if not data:
            return None
        pairs = data.get("pairs") if isinstance(data, dict) else None
        return _top_price_from_pairs(pairs)
    except Exception as e:
        print("Error _price_from_dexscreener_search:", e)
        return None

def _price_from_coingecko_contract(token_addr):
    # CoinGecko token price by contract on Cronos (contract_addresses param expects lower-case)
    try:
        addr = token_addr.lower()
        url = "https://api.coingecko.com/api/v3/simple/token_price/cronos"
        params = {"contract_addresses": addr, "vs_currencies": "usd"}
        r = SESSION.get(url, params=params, timeout=12)
        data = safe_json(r)
        if not data:
            return None
        # data keys are the contract addresses (lowercase)
        val = None
        if isinstance(data, dict):
            # try direct key
            v = data.get(addr)
            if v and "usd" in v:
                val = v["usd"]
            else:
                # sometimes response key casing may vary; try find
                for k, vv in data.items():
                    if k.lower() == addr and isinstance(vv, dict) and "usd" in vv:
                        val = vv["usd"]
                        break
        if val is not None:
            try:
                return float(val)
            except Exception:
                return None
    except Exception as e:
        print("Error _price_from_coingecko_contract:", e)
    return None

def _price_from_coingecko_ids_for_cro():
    # try a couple of CoinGecko ids that may represent Cronos native token
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        ids = "cronos,crypto-com-chain"
        r = SESSION.get(url, params={"ids": ids, "vs_currencies": "usd"}, timeout=8)
        data = safe_json(r)
        if not data:
            return None
        # prefer 'cronos', fallback to 'crypto-com-chain'
        for idk in ("cronos", "crypto-com-chain"):
            if idk in data and "usd" in data[idk]:
                try:
                    return float(data[idk]["usd"])
                except Exception:
                    continue
    except Exception as e:
        print("Error _price_from_coingecko_ids_for_cro:", e)
    return None

def get_price_usd(symbol_or_addr: str):
    """
    Robust price fetch:
      - accepts "CRO" / "cro" or contract address "0x..."
      - uses Dexscreener tokens endpoint, Dexscreener search, and CoinGecko contract price as fallback
    Caches results for PRICE_CACHE_TTL seconds.
    """
    if not symbol_or_addr:
        return None
    key = symbol_or_addr.strip().lower()

    # cache
    now_ts = time.time()
    cached = PRICE_CACHE.get(key)
    if cached:
        price, ts = cached
        if now_ts - ts < PRICE_CACHE_TTL:
            return price

    price = None

    # CRO path
    if key in ("cro", "wcro", "w-cro", "wrappedcro", "wrapped cro"):
        # try dexscreener search first for CRO/USDT or WCRO/USDT
        price = _price_from_dexscreener_search("cro usdt") or _price_from_dexscreener_search("wcro usdt")
        if not price:
            price = _price_from_coingecko_ids_for_cro()

    # if looks like an address -> token contract path
    elif key.startswith("0x") and len(key) == 42:
        # try dexscreener tokens endpoint (best)
        price = _price_from_dexscreener_token(key)
        if not price:
            # try coingecko contract price
            price = _price_from_coingecko_contract(key)
        if not price:
            # last resort: search by contract on dexscreener search
            price = _price_from_dexscreener_search(key)

    else:
        # treat as symbol or free text: try search on dexscreener
        price = _price_from_dexscreener_search(key)
        # if not found, try searching 'symbol usdt' (common)
        if not price and len(key) <= 8:
            price = _price_from_dexscreener_search(f"{key} usdt")

    # store in cache (even None for short TTL)
    try:
        PRICE_CACHE[key] = (price, now_ts)
    except Exception:
        pass

    if price is None:
        # helpful debug printed once per call: show why earlier USD were 0
        print(f"Price lookup failed for '{symbol_or_addr}' (returned None).")
    return price

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

def fetch_latest_token_txs(limit=50):
    """ERC20 transfers for the wallet."""
    if not WALLET_ADDRESS or not ETHERSCAN_API:
        return []
    params = {
        "chainid": CRONOS_CHAINID,
        "module": "account",
        "action": "tokentx",
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
        print("Unexpected token tx response:", str(data)[:600])
        return []
    except Exception as e:
        print("Error fetching token txs:", e)
        return []

def _append_ledger(entry: dict):
    with _day_ledger_lock:
        path = data_file_for_today()
        data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0})
        data["entries"].append(entry)
        # net USD flow: incoming positive, outgoing negative (already signed in entry["usd_value"])
        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
        write_json(path, data)

def _format_amount(a):
    # nice short formatter
    if a is None:
        return "0"
    try:
        a = float(a)
    except Exception:
        return str(a)
    if abs(a) >= 1:
        return f"{a:,.4f}"
    if abs(a) >= 0.0001:
        return f"{a:.6f}"
    return f"{a:.8f}"

def handle_native_tx(tx: dict):
    """
    Handle native CRO transfer from txlist.
    """
    h = tx.get("hash")
    if not h or h in _seen_tx_hashes:
        return
    _seen_tx_hashes.add(h)

    val_raw = tx.get("value", "0")
    # CRO has 18 decimals
    try:
        amount_cro = int(val_raw) / 10**18
    except Exception:
        try:
            amount_cro = float(val_raw)
        except Exception:
            amount_cro = 0.0

    frm  = (tx.get("from") or "").lower()
    to   = (tx.get("to") or "").lower()
    ts   = int(tx.get("timeStamp") or 0)
    dt   = datetime.fromtimestamp(ts) if ts > 0 else now_dt()

    # direction relative to our wallet
    sign = 0
    if to == WALLET_ADDRESS:
        sign = +1  # incoming
    elif frm == WALLET_ADDRESS:
        sign = -1  # outgoing

    if sign == 0 or amount_cro == 0:
        return

    price = get_price_usd("CRO") or 0.0
    usd_value = sign * amount_cro * price

    # update CRO balance
    _token_balances["CRO"] += sign * amount_cro

    # save meta for CRO
    _token_meta["CRO"] = {"symbol": "CRO", "decimals": 18}

    link = CRONOS_TX.format(txhash=h)
    msg = (
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\n"
        f"Hash: {link}\n"
        f"Time: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount_cro:.6f} CRO  (~${_format_amount(amount_cro*price)})"
    )
    print("Sending wallet alert for", h)
    send_telegram(msg)

    entry = {
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h,
        "type": "native",
        "token": "CRO",
        "token_addr": None,
        "amount": sign * amount_cro,
        "price_usd": price,
        "usd_value": usd_value,
        "from": frm,
        "to": to,
    }
    _append_ledger(entry)

def handle_erc20_tx(t: dict):
    """
    Handle ERC20 transfer from tokentx.
    """
    h = t.get("hash")
    if not h:
        return
    # tokentx can include multiple rows for same tx if multiple token transfers.
    # we will not de-dupe by hash here; treat each row as a movement of that token.
    frm  = (t.get("from") or "").lower()
    to   = (t.get("to") or "").lower()
    if WALLET_ADDRESS not in (frm, to):
        return

    token_addr = (t.get("contractAddress") or "").lower()
    symbol     = t.get("tokenSymbol") or token_addr[:8]
    try:
        decimals = int(t.get("tokenDecimal") or 18)
    except Exception:
        decimals = 18

    val_raw = t.get("value", "0")
    try:
        amount = int(val_raw) / (10 ** decimals)
    except Exception:
        try:
            amount = float(val_raw)
        except Exception:
            amount = 0.0

    ts = int(t.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(ts) if ts > 0 else now_dt()

    sign = +1 if to == WALLET_ADDRESS else -1
    price = get_price_usd(token_addr) or 0.0
    usd_value = sign * amount * price

    # track balances/meta
    _token_balances[token_addr] += sign * amount
    _token_meta[token_addr] = {"symbol": symbol, "decimals": decimals}

    link = CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Token TX* ({'IN' if sign>0 else 'OUT'}) {symbol}\n"
        f"Hash: {link}\n"
        f"Time: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {symbol}  (~${_format_amount(abs(amount*price))})"
    )

    entry = {
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h,
        "type": "erc20",
        "token": symbol,
        "token_addr": token_addr,
        "amount": sign * amount,
        "price_usd": price,
        "usd_value": usd_value,
        "from": frm,
        "to": to,
    }
    _append_ledger(entry)

def wallet_monitor_loop():
    global _seen_tx_hashes
    print("Wallet monitor starting; loading initial recent txs...")

    # seed native tx hashes to avoid spamming on first run
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

    last_tokentx_seen = set()  # simple rolling set for last N token tx hashes
    while True:
        # Native txs
        txs = fetch_latest_wallet_txs(limit=25)
        if not txs:
            print("No native txs returned; retrying...")
        else:
            for tx in reversed(txs):  # oldest → newest
                if not isinstance(tx, dict):
                    continue
                handle_native_tx(tx)

        # ERC20 transfers
        toks = fetch_latest_token_txs(limit=50)
        if toks:
            for t in reversed(toks):
                h = t.get("hash")
                if h and h in last_tokentx_seen:
                    continue
                handle_erc20_tx(t)
                if h:
                    last_tokentx_seen.add(h)
            # keep set bounded
            if len(last_tokentx_seen) > 500:
                # drop old roughly
                last_tokentx_seen = set(list(last_tokentx_seen)[-300:])

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
    if not first:
        return None
    pct = (last - first) / first * 100.0
    return pct if abs(pct) >= SPIKE_THRESHOLD else None

def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        print("No tracked pairs; monitor waits until discovery/seed adds some.")
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
            if price_val is not None and price_val > 0:
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
                if str(p.get("chainId", "")).lower() != "cronos":
                    continue
                pair_addr = p.get("pairAddress")
                if not pair_addr:
                    continue
                s = slug("cronos", pair_addr)
                if s in _tracked_pairs:
                    continue
                ensure_tracking_pair("cronos", pair_addr, meta=p)
                adopted += 1
                if adopted >= DISCOVER_LIMIT:
                    break
        except Exception as e:
            print("Discovery error:", e)

        time.sleep(DISCOVER_POLL)

# ========================= Reporting (intraday + end-of-day) =========================
def compute_holdings_usd():
    """
    Returns (total_usd, breakdown list)
    breakdown items: {"token","token_addr","amount","price_usd","usd_value"}
    """
    total = 0.0
    breakdown = []
    # CRO
    cro_amt = _token_balances.get("CRO", 0.0)
    if abs(cro_amt) > 0:
        cro_price = get_price_usd("CRO") or 0.0
        cro_val = cro_amt * cro_price
        total += cro_val
        breakdown.append({
            "token": "CRO", "token_addr": None, "amount": cro_amt,
            "price_usd": cro_price, "usd_value": cro_val
        })
    # Tokens
    for addr, amt in list(_token_balances.items()):
        if addr == "CRO":
            continue
        if abs(amt) <= 0:
            continue
        meta = _token_meta.get(addr, {})
        sym = meta.get("symbol") or addr[:8]
        price = get_price_usd(addr) or 0.0
        val = amt * price
        total += val
        breakdown.append({
            "token": sym, "token_addr": addr, "amount": amt,
            "price_usd": price, "usd_value": val
        })
    return total, breakdown

def sum_month_net_flows():
    """
    Sum net_usd_flow from all daily files in current month.
    """
    pref = month_prefix()
    total = 0.0
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json") and pref in fn:
                data = read_json(os.path.join(DATA_DIR, fn), default=None)
                if isinstance(data, dict):
                    total += float(data.get("net_usd_flow", 0.0))
    except Exception:
        pass
    return total

def build_day_report_text():
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0})
    entries = data.get("entries", [])
    net_flow = float(data.get("net_usd_flow", 0.0))

    lines = [f"*📒 Daily Report* ({data.get('date')})"]
    if not entries:
        lines.append("_No transactions today._")
    else:
        lines.append("*Transactions:*")
        # keep last ~20 lines for brevity; if more, summarize count
        MAX_LINES = 20
        for i, e in enumerate(entries[-MAX_LINES:]):
            tok = e.get("token") or "?"
            amt = e.get("amount") or 0
            usd = e.get("usd_value") or 0
            tm  = e.get("time","")[-8:]
            direction = "IN" if float(amt) > 0 else "OUT"
            lines.append(f"• {tm} — {direction} {tok} { _format_amount(amt) }  (${_format_amount(usd)})")
        if len(entries) > MAX_LINES:
            lines.append(f"_…and {len(entries)-MAX_LINES} earlier txs._")

    lines.append(f"\n*Net PnL (USDT) today:* ${_format_amount(net_flow)}")

    holdings_total, breakdown = compute_holdings_usd()
    lines.append(f"*Holdings value now:* ${_format_amount(holdings_total)}")
    if breakdown:
        for b in breakdown[:10]:
            lines.append(f"  – {b['token']}: { _format_amount(b['amount']) } @ ${_format_amount(b['price_usd'])} = ${_format_amount(b['usd_value'])}")
        if len(breakdown) > 10:
            lines.append(f"  …and {len(breakdown)-10} more.")

    month_total = sum_month_net_flows()
    lines.append(f"*Month PnL (USDT):* ${_format_amount(month_total)}")

    return "\n".join(lines)

def intraday_report_loop():
    global _last_intraday_sent
    # initial small delay so wallet threads warm up
    time.sleep(10)
    send_telegram("⏱ Intraday reporting enabled.")
    while True:
        now = now_dt()
        # send every INTRADAY_HOURS
        if time.time() - _last_intraday_sent >= INTRADAY_HOURS * 3600:
            try:
                txt = build_day_report_text()
                send_telegram("🟡 *Intraday Update*\n" + txt)
            except Exception as e:
                print("Intraday report error:", e)
            _last_intraday_sent = time.time()
        time.sleep(30)

def end_of_day_scheduler_loop():
    send_telegram(f"🕛 End-of-day scheduler active (at {EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ}).")
    while True:
        now = now_dt()
        target = now.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        wait_s = (target - now).total_seconds()
        # sleep in chunks to be interrupt-friendly
        while wait_s > 0:
            s = min(wait_s, 30)
            time.sleep(s)
            wait_s -= s
        # time to send daily report
        try:
            txt = build_day_report_text()
            send_telegram("🟢 *End of Day Report*\n" + txt)
        except Exception as e:
            print("EOD report error:", e)

# ========================= Dexscreener monitor core (existing) =========================
def monitor_tracked_pairs_loop_wrapper():
    # wrap to ensure it doesn't crash the whole app
    try:
        monitor_tracked_pairs_loop()
    except Exception as e:
        print("Pairs monitor crashed:", e)
        time.sleep(3)

# ========================= Entrypoint =========================
def main():
    print("Starting monitor with config:")
    print("WALLET_ADDRESS:", WALLET_ADDRESS)
    print("TELEGRAM_BOT_TOKEN present:", bool(TELEGRAM_BOT_TOKEN))
    print("TELEGRAM_CHAT_ID:", TELEGRAM_CHAT_ID)
    print("ETHERSCAN_API present:", bool(ETHERSCAN_API))
    print("DEX_PAIRS:", DEX_PAIRS)
    print("DISCOVER_ENABLED:", DISCOVER_ENABLED, "| DISCOVER_QUERY:", DISCOVER_QUERY)
    print("TZ:", TZ, "| INTRADAY_HOURS:", INTRADAY_HOURS, "| EOD:", f"{EOD_HOUR:02d}:{EOD_MINUTE:02d}")

    # Threads
    t_wallet  = threading.Thread(target=wallet_monitor_loop, daemon=True)
    t_pairs   = threading.Thread(target=monitor_tracked_pairs_loop_wrapper, daemon=True)
    t_discover= threading.Thread(target=discovery_loop, daemon=True)
    t_intraday= threading.Thread(target=intraday_report_loop, daemon=True)
    t_eod     = threading.Thread(target=end_of_day_scheduler_loop, daemon=True)

    t_wallet.start()
    t_pairs.start()
    t_discover.start()
    t_intraday.start()
    t_eod.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Stopping monitors.")

if __name__ == "__main__":
    main()
