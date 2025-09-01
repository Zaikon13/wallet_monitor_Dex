#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Cronos wallet + Dexscreener monitor
Features:
- Wallet monitor (Cronos via Etherscan Multichain)
- Token tx processing (IN/OUT), position & avg-cost PnL (realized/unrealized)
- Dexscreener pair discovery + pair monitoring (spike / price-move alerts)
- ATH tracking per token
- Intraday (every INTRADAY_HOURS) & End-of-Day report (EOD_HOUR:EOD_MINUTE)
- Swap reconciliation helper (simple heuristic)
- Persists daily ledger to /app/data/transactions_YYYY-MM-DD.json
- Telegram alerts for txs, spikes, ATH, intraday & EOD reports
"""

import os
import time
import threading
import json
import math
from collections import deque, defaultdict
from datetime import datetime, timedelta
import requests
from dotenv import load_dotenv

load_dotenv()

# ========================= Environment (Railway exact names) =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")      # Etherscan multichain key
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")      # optional seeding "cronos/0xPAIR1,cronos/0xPAIR2"
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL           = int(os.getenv("DEX_POLL", "60"))

# Optional defaults already in Railway envs you provided
PRICE_WINDOW          = int(os.getenv("PRICE_WINDOW", "3"))
SPIKE_THRESHOLD       = float(os.getenv("SPIKE_THRESHOLD", "8.0"))
MIN_VOLUME_FOR_ALERT  = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))
DISCOVER_ENABLED      = os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on")
DISCOVER_QUERY        = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT        = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL         = int(os.getenv("DISCOVER_POLL", "120"))
TOKENS                = os.getenv("TOKENS", "")   # optional "cronos/0xTokenA, ..."
TZ                    = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS        = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR              = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE            = int(os.getenv("EOD_MINUTE", "59"))

# ========================= Constants =========================
ETHERSCAN_V2_URL   = "https://api.etherscan.io/v2/api"   # multichain endpoint (chainid param)
CRONOS_CHAINID     = 25
DEX_BASE_PAIRS     = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS    = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH    = "https://api.dexscreener.com/latest/dex/search"
DEXSITE_PAIR       = "https://dexscreener.com/{chain}/{pair}"
CRONOS_TX          = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL       = "https://api.telegram.org/bot{token}/sendMessage"
DATA_DIR           = "/app/data"

# create data dir
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

# Global HTTP session
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})

# ========================= State =========================
_seen_tx_hashes    = set()
_last_prices       = {}          # slug -> float
_price_history     = {}          # slug -> deque
_last_pair_tx      = {}          # slug -> last tx hash
_tracked_pairs     = set()       # slugs
_known_pairs_meta   = {}         # slug -> meta dict

_day_ledger_lock   = threading.Lock()
_token_balances    = defaultdict(float)    # token_addr or "CRO" -> amount
_token_meta        = {}                   # token_addr -> {"symbol","decimals"}
_position_qty      = defaultdict(float)   # token_key -> qty held (avg-cost)
_position_cost     = defaultdict(float)   # token_key -> total USD cost of held qty
_realized_pnl_today = 0.0
EPSILON            = 1e-12
_last_intraday_sent = 0.0

PRICE_CACHE = {}
PRICE_CACHE_TTL = 60  # seconds cache for price lookups

# ========================= Utils =========================
def now_dt():
    return datetime.now()

def ymd(dt=None):
    if dt is None:
        dt = now_dt()
    return dt.strftime("%Y-%m-%d")

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

def _format_amount(a):
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

# ========================= Telegram helper =========================
_rate_limit_last = 0.0
def send_telegram(message: str) -> bool:
    global _rate_limit_last
    now = time.time()
    # soft rate limit ~0.8s
    if now - _rate_limit_last < 0.8:
        time.sleep(0.8 - (now - _rate_limit_last))
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured.")
        return False
    url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = SESSION.post(url, data=payload, timeout=12)
        _rate_limit_last = time.time()
        if r.status_code == 401:
            print("❌ Telegram 401 Unauthorized. Regenerate token.")
            return False
        if r.status_code != 200:
            print("⚠️ Telegram API returned", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        print("Exception sending telegram:", e)
        return False

def safe_json(r):
    if r is None:
        return None
    if not getattr(r, "ok", False):
        print("HTTP non-ok:", getattr(r, "status_code", None))
        return None
    try:
        return r.json()
    except Exception:
        txt = (r.text[:800].replace("\n", " ")) if hasattr(r, "text") else "<no body>"
        print("Response not JSON (preview):", txt)
        return None

# ========================= Price helpers (Dexscreener + CoinGecko) =========================
def _top_price_from_pairs_pricehelpers(pairs):
    if not pairs:
        return None
    best = None
    best_liq = -1.0
    for p in pairs:
        try:
            chain_id = str(p.get("chainId","")).lower()
            if chain_id and chain_id != "cronos":
                continue
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
    try:
        url = f"{DEX_BASE_TOKENS}/cronos/{token_addr}"
        r = SESSION.get(url, timeout=10)
        data = safe_json(r)
        if not data:
            return None
        pairs = data.get("pairs") if isinstance(data, dict) else None
        return _top_price_from_pairs_pricehelpers(pairs)
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
        return _top_price_from_pairs_pricehelpers(pairs)
    except Exception as e:
        print("Error _price_from_dexscreener_search:", e)
        return None

def _price_from_coingecko_contract(token_addr):
    try:
        addr = token_addr.lower()
        url = "https://api.coingecko.com/api/v3/simple/token_price/cronos"
        params = {"contract_addresses": addr, "vs_currencies": "usd"}
        r = SESSION.get(url, params=params, timeout=10)
        data = safe_json(r)
        if not data:
            return None
        v = data.get(addr)
        if v and "usd" in v:
            return float(v["usd"])
    except Exception as e:
        print("Error _price_from_coingecko_contract:", e)
    return None

def _price_from_coingecko_ids_for_cro():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        ids = "cronos,crypto-com-chain"
        r = SESSION.get(url, params={"ids": ids, "vs_currencies": "usd"}, timeout=8)
        data = safe_json(r)
        if not data:
            return None
        for idk in ("cronos", "crypto-com-chain"):
            if idk in data and "usd" in data[idk]:
                return float(data[idk]["usd"])
    except Exception as e:
        print("Error _price_from_coingecko_ids_for_cro:", e)
    return None

def get_price_usd(symbol_or_addr: str):
    """
    Accepts:
     - CRO / symbols
     - contract address '0x...'
    """
    if not symbol_or_addr:
        return None
    key = symbol_or_addr.strip().lower()
    now_ts = time.time()
    cached = PRICE_CACHE.get(key)
    if cached:
        price, ts = cached
        if now_ts - ts < PRICE_CACHE_TTL:
            return price

    price = None
    if key in ("cro", "wcro", "w-cro", "wrappedcro"):
        price = _price_from_dexscreener_search("cro usdt") or _price_from_dexscreener_search("wcro usdt")
        if not price:
            price = _price_from_coingecko_ids_for_cro()
    elif key.startswith("0x") and len(key) == 42:
        price = _price_from_dexscreener_token(key)
        if not price:
            price = _price_from_coingecko_contract(key)
        if not price:
            price = _price_from_dexscreener_search(key)
    else:
        price = _price_from_dexscreener_search(key)
        if not price and len(key) <= 8:
            price = _price_from_dexscreener_search(f"{key} usdt")

    try:
        PRICE_CACHE[key] = (price, now_ts)
    except Exception:
        pass

    if price is None:
        # do not spam logs; return 0.0 fallback
        return 0.0
    return price

# ========================= Wallet (Cronos via Etherscan Multichain) =========================
def fetch_latest_wallet_txs(limit=50):
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
        # etherscan v2 multichain sometimes returns different shapes; attempt fallback
        return []
    except Exception as e:
        print("Error fetching wallet txs:", e)
        return []

def fetch_latest_token_txs(limit=100):
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
        return []
    except Exception as e:
        print("Error fetching token txs:", e)
        return []

# ========================= Ledger helpers & Cost-basis (avg-cost) =========================
def _append_ledger(entry: dict):
    with _day_ledger_lock:
        path = data_file_for_today()
        data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        data["entries"].append(entry)
        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
        data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0))
        write_json(path, data)

def _replay_today_cost_basis():
    """Rebuild cost-basis & realized PnL from today's ledger on startup/restart."""
    global _position_qty, _position_cost, _realized_pnl_today
    _position_qty.clear(); _position_cost.clear(); _realized_pnl_today = 0.0
    path = data_file_for_today()
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        return
    for e in data.get("entries", []):
        key = "CRO" if (e.get("token_addr") in (None, "", "None") and e.get("token") == "CRO") else (e.get("token_addr") or "CRO")
        amt = float(e.get("amount") or 0.0)
        price = float(e.get("price_usd") or 0.0)
        _update_cost_basis(key, amt, price)
    # update file realized (idempotent)
    try:
        total_real = sum(float(e.get("realized_pnl", 0.0)) for e in data.get("entries", []))
        data["realized_pnl"] = total_real
        write_json(path, data)
    except Exception:
        pass

def _update_cost_basis(token_key: str, signed_amount: float, price_usd: float):
    """
    Avg-cost model.
    Positive amount => buy (increase qty + cost)
    Negative amount => sell (realize PnL up to held qty; no shorting)
    Returns realized_pnl for this movement (can be 0).
    """
    global _realized_pnl_today
    qty = _position_qty[token_key]
    cost = _position_cost[token_key]
    realized = 0.0
    if signed_amount > EPSILON:
        buy_qty = signed_amount
        add_cost = buy_qty * (price_usd or 0.0)
        _position_qty[token_key] = qty + buy_qty
        _position_cost[token_key] = cost + add_cost
    elif signed_amount < -EPSILON:
        sell_qty_req = -signed_amount
        if qty <= EPSILON:
            sell_qty = 0.0
        else:
            sell_qty = min(sell_qty_req, qty)
            avg_cost = (cost / qty) if qty > EPSILON else (price_usd or 0.0)
            realized = (price_usd - avg_cost) * sell_qty
            _position_qty[token_key] = qty - sell_qty
            _position_cost[token_key] = max(0.0, cost - avg_cost * sell_qty)
    _realized_pnl_today += realized
    return realized

# ========================= Handlers (native + erc20) =========================
def handle_native_tx(tx: dict):
    h = tx.get("hash")
    if not h or h in _seen_tx_hashes:
        return
    _seen_tx_hashes.add(h)

    val_raw = tx.get("value", "0")
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

    sign = 0
    if to == WALLET_ADDRESS:
        sign = +1
    elif frm == WALLET_ADDRESS:
        sign = -1

    if sign == 0 or abs(amount_cro) <= EPSILON:
        return

    price = get_price_usd("CRO") or 0.0
    usd_value = sign * amount_cro * price

    # update CRO balance
    _token_balances["CRO"] += sign * amount_cro
    _token_meta["CRO"] = {"symbol": "CRO", "decimals": 18}

    realized = _update_cost_basis("CRO", sign * amount_cro, price)

    link = CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\n"
        f"Hash: {link}\n"
        f"Time: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount_cro:.6f} CRO  (~${_format_amount(amount_cro*price)})"
    )

    entry = {
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h,
        "type": "native",
        "token": "CRO",
        "token_addr": None,
        "amount": sign * amount_cro,
        "price_usd": price,
        "usd_value": usd_value,
        "realized_pnl": realized,
        "from": frm,
        "to": to,
    }
    _append_ledger(entry)

def handle_erc20_tx(t: dict):
    h = t.get("hash")
    if not h:
        return
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

    _token_balances[token_addr] += sign * amount
    _token_meta[token_addr] = {"symbol": symbol, "decimals": decimals}

    realized = _update_cost_basis(token_addr, sign * amount, price)

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
        "realized_pnl": realized,
        "from": frm,
        "to": to,
    }
    _append_ledger(entry)

# ========================= Wallet monitor loop =========================
def wallet_monitor_loop():
    global _seen_tx_hashes
    print("Wallet monitor starting; loading initial recent txs...")
    initial = fetch_latest_wallet_txs(limit=100)
    try:
        _seen_tx_hashes = set(tx.get("hash") for tx in initial if isinstance(tx, dict) and tx.get("hash"))
    except Exception:
        _seen_tx_hashes = set()

    _replay_today_cost_basis()

    send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}` (Cronos).")

    last_tokentx_seen = set()
    while True:
        # Native txs
        txs = fetch_latest_wallet_txs(limit=50)
        if txs:
            for tx in reversed(txs):  # oldest -> newest
                try:
                    handle_native_tx(tx)
                except Exception as e:
                    print("handle_native_tx error:", e)

        # ERC20 transfers
        toks = fetch_latest_token_txs(limit=200)
        if toks:
            for t in reversed(toks):
                h = t.get("hash")
                if h and h in last_tokentx_seen:
                    continue
                try:
                    handle_erc20_tx(t)
                except Exception as e:
                    print("handle_erc20_tx error:", e)
                if h:
                    last_tokentx_seen.add(h)
            if len(last_tokentx_seen) > 1000:
                last_tokentx_seen = set(list(last_tokentx_seen)[-500:])

        time.sleep(WALLET_POLL)

# ========================= Dexscreener helpers & monitors =========================
def slug(chain: str, pair_address: str) -> str:
    return f"{chain}/{pair_address}".lower()

def fetch_pair(slug_str: str):
    url = f"{DEX_BASE_PAIRS}/{slug_str}"
    try:
        r = SESSION.get(url, timeout=12)
        return safe_json(r)
    except Exception as e:
        print("Error fetching pair", slug_str, e)
        return None

def fetch_token_pairs(chain: str, token_address: str):
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
        ds_link = DEXSITE_PAIR.format(chain=chain, pair=pair_address)
        sym = None
        if isinstance(meta, dict):
            bt = meta.get("baseToken") or {}
            sym = bt.get("symbol")
        title = f"{sym} ({s})" if sym else s
        send_telegram(f"🆕 Now monitoring pair: {title}\n{ds_link}")

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
            # normalise
            pair = None
            if isinstance(data, dict) and isinstance(data.get("pair"), dict):
                pair = data["pair"]
            elif isinstance(data, dict) and isinstance(data.get("pairs"), list) and data["pairs"]:
                pair = data["pairs"][0]
            else:
                continue

            try:
                price_val = float(pair.get("priceUsd") or 0)
            except Exception:
                price_val = None

            vol_h1 = None
            vol = pair.get("volume") or {}
            if isinstance(vol, dict):
                try:
                    vol_h1 = float(vol.get("h1") or 0)
                except Exception:
                    vol_h1 = None

            symbol = (pair.get("baseToken") or {}).get("symbol") or s

            if price_val and price_val > 0:
                update_price_history(s, price_val)
                spike_pct = detect_spike(s)
                if spike_pct is not None:
                    if MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 < MIN_VOLUME_FOR_ALERT:
                        pass
                    else:
                        send_telegram(f"🚨 Spike on {symbol}: {spike_pct:.2f}% over recent samples\nPrice: ${price_val:.6f} Vol1h: {vol_h1}")
                        _price_history[s].clear()
                        _last_prices[s] = price_val

            prev = _last_prices.get(s)
            if prev is not None and price_val is not None and prev != 0:
                delta = (price_val - prev) / prev * 100.0
                if abs(delta) >= PRICE_MOVE_THRESHOLD:
                    send_telegram(f"📈 Price move on {symbol}: {delta:.2f}%\nPrice: ${price_val:.6f} (prev ${prev:.6f})")
                    _last_prices[s] = price_val

            # last tx detection
            last_tx = pair.get("lastTx") or {}
            last_tx_hash = last_tx.get("hash") if isinstance(last_tx, dict) else None
            if last_tx_hash:
                prev_tx = _last_pair_tx.get(s)
                if prev_tx != last_tx_hash:
                    _last_pair_tx[s] = last_tx_hash
                    send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx_hash)}")

        time.sleep(DEX_POLL)

# ========================= Discovery loop =========================
def discovery_loop():
    # seed from TOKENS env
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

    # seed from DEX_PAIRS env (optional)
    seeds = [p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    for s in seeds:
        if s.startswith("cronos/"):
            ensure_tracking_pair("cronos", s.split("/",1)[1])

    if not DISCOVER_ENABLED:
        print("Discovery disabled.")
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

# ========================= Reporting =========================
def compute_holdings_usd():
    total = 0.0
    breakdown = []
    unrealized = 0.0

    # CRO native
    cro_amt = max(0.0, _token_balances.get("CRO", 0.0))
    if cro_amt > EPSILON:
        cro_price = get_price_usd("CRO") or 0.0
        cro_val = cro_amt * cro_price
        total += cro_val
        breakdown.append({"token":"CRO","token_addr":None,"amount":cro_amt,"price_usd":cro_price,"usd_value":cro_val})
        rem_qty = _position_qty.get("CRO",0.0)
        rem_cost= _position_cost.get("CRO",0.0)
        if rem_qty > EPSILON:
            unrealized += (cro_val - rem_cost)

    for addr, amt in list(_token_balances.items()):
        if addr == "CRO":
            continue
        amt = max(0.0, amt)
        if amt <= EPSILON:
            continue
        meta = _token_meta.get(addr,{})
        sym = meta.get("symbol") or addr[:8]
        price = 0.0
        if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
            price = get_price_usd(addr) or 0.0
        else:
            price = get_price_usd(sym) or get_price_usd(addr) or 0.0
        val = amt * price
        total += val
        breakdown.append({"token":sym,"token_addr":addr,"amount":amt,"price_usd":price,"usd_value":val})
        rem_qty = _position_qty.get(addr,0.0)
        rem_cost= _position_cost.get(addr,0.0)
        if rem_qty > EPSILON:
            unrealized += (val - rem_cost)
    return total, breakdown, unrealized

def sum_month_net_flows_and_realized():
    pref = datetime.now().strftime("%Y-%m")
    total_flow = 0.0
    total_real = 0.0
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json") and pref in fn:
                data = read_json(os.path.join(DATA_DIR, fn), default=None)
                if isinstance(data, dict):
                    total_flow += float(data.get("net_usd_flow", 0.0))
                    total_real += float(data.get("realized_pnl", 0.0))
    except Exception:
        pass
    return total_flow, total_real

def build_day_report_text():
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])
    net_flow = float(data.get("net_usd_flow", 0.0))
    realized_today = float(data.get("realized_pnl", 0.0))

    per_asset_flow = defaultdict(float)
    per_asset_real = defaultdict(float)
    per_asset_amt  = defaultdict(float)
    per_asset_last_price = {}
    per_asset_token_addr = {}

    for e in entries:
        tok = e.get("token") or "?"
        addr = e.get("token_addr")
        per_asset_flow[tok] += float(e.get("usd_value") or 0.0)
        per_asset_real[tok] += float(e.get("realized_pnl") or 0.0)
        per_asset_amt[tok]  += float(e.get("amount") or 0.0)
        if float(e.get("price_usd", 0.0)) > 0:
            per_asset_last_price[tok] = float(e.get("price_usd"))
        if addr and tok not in per_asset_token_addr:
            per_asset_token_addr[tok] = addr

    lines = [f"*📒 Daily Report* ({data.get('date')})"]
    if not entries:
        lines.append("_No transactions today._")
    else:
        lines.append("*Transactions:*")
        MAX_LINES = 20
        for i, e in enumerate(entries[-MAX_LINES:]):
            tok = e.get("token") or "?"
            amt = e.get("amount") or 0
            usd = e.get("usd_value") or 0
            tm  = e.get("time","")[-8:]
            direction = "IN" if float(amt) > 0 else "OUT"
            unit_price = e.get("price_usd") or 0.0
            pnl_line = ""
            try:
                rp = float(e.get("realized_pnl", 0.0))
                if abs(rp) > 1e-9:
                    pnl_line = f"  PnL: ${_format_amount(rp)}"
            except Exception:
                pass
            lines.append(
                f"• {tm} — {direction} {tok} {_format_amount(amt)}  @ ${_format_amount(unit_price)}  (${_format_amount(usd)}){pnl_line}"
            )
        if len(entries) > MAX_LINES:
            lines.append(f"_…and {len(entries)-MAX_LINES} earlier txs._")

    lines.append(f"\n*Net USD flow today:* ${_format_amount(net_flow)}")
    lines.append(f"*Realized PnL today:* ${_format_amount(realized_today)}")

    holdings_total, breakdown, unrealized = compute_holdings_usd()
    lines.append(f"*Holdings (MTM) now:* ${_format_amount(holdings_total)}")
    if breakdown:
        for b in breakdown[:12]:
            lines.append(f"  – {b['token']}: { _format_amount(b['amount']) } @ ${_format_amount(b['price_usd'])} = ${_format_amount(b['usd_value'])}")
        if len(breakdown) > 12:
            lines.append(f"  …and {len(breakdown)-12} more.")
    lines.append(f"*Unrealized PnL (open positions):* ${_format_amount(unrealized)}")

    if per_asset_flow:
        lines.append("\n*Per-Asset Summary (Today):*")
        order = sorted(per_asset_flow.items(), key=lambda kv: abs(kv[1]), reverse=True)
        LIMIT = 12
        for idx, (tok, flow) in enumerate(order[:LIMIT]):
            addr = per_asset_token_addr.get(tok)
            live_price = per_asset_last_price.get(tok)
            if live_price is None:
                if tok.upper() == "CRO":
                    live_price = get_price_usd("CRO") or 0.0
                elif addr and isinstance(addr, str) and addr.startswith("0x"):
                    live_price = _price_from_dexscreener_token(addr) or 0.0
                else:
                    live_price = get_price_usd(tok) or 0.0
            amt_sum = per_asset_amt.get(tok, 0.0)
            real_sum = per_asset_real.get(tok, 0.0)
            lines.append(
                f"  • {tok}: flow ${_format_amount(flow)} | realized ${_format_amount(real_sum)} | today qty {_format_amount(amt_sum)} | price ${_format_amount(live_price)}"
            )
        if len(order) > LIMIT:
            lines.append(f"  …and {len(order)-LIMIT} more.")

    month_flow, month_real = sum_month_net_flows_and_realized()
    lines.append(f"\n*Month Net Flow:* ${_format_amount(month_flow)}")
    lines.append(f"*Month Realized PnL:* ${_format_amount(month_real)}")

    return "\n".join(lines)

def intraday_report_loop():
    global _last_intraday_sent
    time.sleep(5)
    send_telegram("⏱ Intraday reporting enabled.")
    while True:
        try:
            if time.time() - _last_intraday_sent >= INTRADAY_HOURS * 3600:
                txt = build_day_report_text()
                send_telegram("🟡 *Intraday Update*\n" + txt)
                _last_intraday_sent = time.time()
        except Exception as e:
            print("Intraday report error:", e)
        time.sleep(30)

def end_of_day_scheduler_loop():
    send_telegram(f"🕛 End-of-day scheduler active (at {EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ}).")
    while True:
        now = now_dt()
        target = now.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        wait_s = (target - now).total_seconds()
        while wait_s > 0:
            s = min(wait_s, 30)
            time.sleep(s)
            wait_s -= s
        try:
            txt = build_day_report_text()
            send_telegram("🟢 *End of Day Report*\n" + txt)
        except Exception as e:
            print("EOD report error:", e)

# ========================= Swap reconciliation helper =========================
def reconcile_swaps_from_entries():
    """
    Heuristic: pair OUT (negative amount) followed shortly by IN of a different token -> treat as a swap pair
    Returns list of tuple((sell_entry, buy_entry))
    """
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])
    swaps = []
    i = 0
    while i < len(entries):
        e = entries[i]
        if float(e.get("amount", 0.0)) < 0:
            j = i + 1
            while j < len(entries) and j <= i + 6:
                e2 = entries[j]
                if float(e2.get("amount", 0.0)) > 0 and e2.get("token") != e.get("token"):
                    swaps.append((e, e2))
                    break
                j += 1
        i += 1
    return swaps

# ========================= Thread runner helper =========================
def run_with_restart(fn, name, daemon=True):
    def runner():
        while True:
            try:
                print(f"Thread {name} starting.")
                fn()
                print(f"Thread {name} exited cleanly.")
                break
            except Exception as e:
                print(f"Thread {name} crashed: {e}. Restarting in 3s...")
                time.sleep(3)
    t = threading.Thread(target=runner, daemon=daemon, name=name)
    t.start()
    return t

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

    # Start threads
    run_with_restart(wallet_monitor_loop, "wallet_monitor")
    run_with_restart(monitor_tracked_pairs_loop, "pairs_monitor")
    run_with_restart(discovery_loop, "discovery")
    run_with_restart(intraday_report_loop, "intraday_report")
    run_with_restart(end_of_day_scheduler_loop, "eod_scheduler")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Stopping monitors.")

if __name__ == "__main__":
    main()
