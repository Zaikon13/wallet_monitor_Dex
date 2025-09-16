#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (patched 2025-09-16)

What’s included (single-file deploy):
- Wallet monitor (Cronos via Etherscan Multichain v2)
- Live TX alerts (native CRO + ERC-20) with Markdown-safe Telegram messages
- Cost-basis (avg-cost) with realized & unrealized PnL
- Real-time MTM valuation (Dexscreener first, CoinGecko fallbacks) + 60s cache
- Intraday report (every INTRADAY_HOURS) & EOD report (EOD_HOUR:EOD_MINUTE TZ)
- Dexscreener pairs monitor (price-move %, spike window, lastTx alerts)
- Auto-discovery for Cronos pairs via /search + token → top pair resolution
- ATH tracker per asset with persistent storage (data/ath.json) + alerts
- Hardening for Dexscreener 403/404 and request headers; graceful fallbacks
- Keeps tCRO strictly separate from CRO (no merging) per project rule

Env (exact names):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WALLET_ADDRESS, ETHERSCAN_API
  DEX_PAIRS, PRICE_MOVE_THRESHOLD, WALLET_POLL, DEX_POLL
Optional:
  PRICE_WINDOW, SPIKE_THRESHOLD, MIN_VOLUME_FOR_ALERT
  DISCOVER_ENABLED, DISCOVER_QUERY, DISCOVER_LIMIT, DISCOVER_POLL, TOKENS
  TZ, INTRADAY_HOURS, EOD_HOUR, EOD_MINUTE

Run (Railway): Procfile → `worker: python main.py`
Reqs: requests, python-dotenv
"""

import os
import time
import threading
from collections import deque, defaultdict
import json
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

# ========================= Environment =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")  # Etherscan Multichain (chainid=25)
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")  # "cronos/0xPAIR1,cronos/0xPAIR2"
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL           = int(os.getenv("DEX_POLL", "60"))

PRICE_WINDOW         = int(os.getenv("PRICE_WINDOW", "3"))
SPIKE_THRESHOLD      = float(os.getenv("SPIKE_THRESHOLD", "8.0"))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

DISCOVER_ENABLED   = os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on")
DISCOVER_QUERY     = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT     = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL      = int(os.getenv("DISCOVER_POLL", "120"))
TOKENS             = os.getenv("TOKENS", "")  # e.g. "cronos/0xTokenA,cronos/0xTokenB"

TZ                 = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS     = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR           = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE         = int(os.getenv("EOD_MINUTE", "59"))

# ========================= Constants =========================
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"  # multichain endpoint
CRONOS_CHAINID   = 25
DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
DEXSITE_PAIR     = "https://dexscreener.com/{chain}/{pair}"
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL     = "https://api.telegram.org/bot{token}/sendMessage"

DATA_DIR         = "/app/data"
ATH_FILE         = os.path.join(DATA_DIR, "ath.json")

# Apply timezone (best effort for EOD scheduling)
try:
    os.environ["TZ"] = TZ
    if hasattr(time, "tzset"):
        time.tzset()
except Exception:
    pass

# ========================= HTTP Session =========================
SESSION = requests.Session()
SESSION.headers.update({
    # Hardened headers to reduce 403s
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://dexscreener.com",
    "Referer": "https://dexscreener.com/",
    "Connection": "keep-alive",
})

# ========================= State =========================
_seen_tx_hashes   = set()
_last_prices      = {}                 # slug -> float
_price_history    = {}                 # slug -> deque
_last_pair_tx     = {}                 # slug -> tx hash
_rate_limit_last  = 0.0
_tracked_pairs    = set()              # {"cronos/0xPAIR"}
_known_pairs_meta = {}

# Ledger / PnL
_day_ledger_lock      = threading.Lock()
_token_balances       = defaultdict(float)  # token_address or "CRO" -> qty
_token_meta           = {}                  # token_addr -> {symbol, decimals}  ; for CRO use key "CRO"
_position_qty         = defaultdict(float)  # token_key -> qty >= 0
_position_cost        = defaultdict(float)  # token_key -> total USD cost for remaining qty
_realized_pnl_today   = 0.0
EPSILON               = 1e-12

# Prices
PRICE_CACHE     = {}
PRICE_CACHE_TTL = 60

# ATH tracker: { token_key (addr or "CRO"): {"ath": float, "ts": iso} }
_ath            = {}

# Ensure data dir
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

# ========================= Helpers =========================

def now_dt():
    return datetime.now()

def ymd(dt=None):
    dt = dt or now_dt()
    return dt.strftime("%Y-%m-%d")

def month_prefix(dt=None):
    dt = dt or now_dt()
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

# -------- Telegram (Markdown-safe) --------
_MD_ESCAPE = str.maketrans({
    "_": "\\_", "*": "\\*", "[": "\\[", "]": "\\]", "(": "\\(", ")": "\\)", "~": "\\~",
    "`": "\\`", ">": "\\>", "#": "\\#", "+": "\\+", "-": "\\-", "=": "\\=", "|": "\\|",
    "{": "\\{", "}": "\\}", ".": "\\.", "!": "\\!",
})

def md(text: str) -> str:
    return (text or "").translate(_MD_ESCAPE)

def send_telegram(message: str) -> bool:
    global _rate_limit_last
    now = time.time()
    if now - _rate_limit_last < 0.8:
        time.sleep(0.8 - (now - _rate_limit_last))
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (missing TELEGRAM_* envs)")
        return False
    url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        # Use MarkdownV2 but escape everything
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": True,
    }
    try:
        r = SESSION.post(url, data=payload, timeout=12)
        _rate_limit_last = time.time()
        if r.status_code == 401:
            print("❌ Telegram 401 Unauthorized. Update TELEGRAM_BOT_TOKEN.")
            print("Response:", r.text[:300])
            raise SystemExit(1)
        if r.status_code != 200:
            print("⚠️ Telegram API", r.status_code, r.text[:300])
            return False
        return True
    except SystemExit:
        raise
    except Exception as e:
        print("Telegram exception:", e)
        return False

# -------- HTTP JSON --------

def safe_json(resp):
    if resp is None:
        return None
    if not getattr(resp, "ok", False):
        print("HTTP error:", getattr(resp, "status_code", None), str(getattr(resp, "text", ""))[:300])
        return None
    try:
        return resp.json()
    except Exception:
        t = getattr(resp, "text", "")
        print("Response not JSON (preview):", (t[:800].replace("\n"," ")))
        return None

# ========================= Pricing (Dexscreener + CoinGecko) =========================

def _top_price_from_pairs(pairs):
    if not pairs:
        return None
    best_price = None
    best_liq = -1.0
    for p in pairs:
        try:
            chain_id = str(p.get("chainId", "")).lower()
            if chain_id and chain_id != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq = liq
                best_price = price
        except Exception:
            continue
    return best_price

def _price_from_dexscreener_token(token_addr):
    try:
        url = f"{DEX_BASE_TOKENS}/cronos/{token_addr}"
        r = SESSION.get(url, timeout=12)
        data = safe_json(r)
        pairs = data.get("pairs") if isinstance(data, dict) else None
        return _top_price_from_pairs(pairs)
    except Exception as e:
        print("Dexscreener token price error:", e)
        return None

def _price_from_dexscreener_search(query):
    try:
        r = SESSION.get(DEX_BASE_SEARCH, params={"q": query}, timeout=12)
        data = safe_json(r)
        pairs = data.get("pairs") if isinstance(data, dict) else None
        return _top_price_from_pairs(pairs)
    except Exception as e:
        print("Dexscreener search price error:", e)
        return None

def _price_from_coingecko_contract(token_addr):
    try:
        addr = token_addr.lower()
        url = "https://api.coingecko.com/api/v3/simple/token_price/cronos"
        params = {"contract_addresses": addr, "vs_currencies": "usd"}
        r = SESSION.get(url, params=params, timeout=10)
        data = safe_json(r)
        if not isinstance(data, dict):
            return None
        row = data.get(addr) or {}
        val = row.get("usd")
        if val is None:
            # Some responses use mixed-case keys
            for k, v in data.items():
                if k.lower() == addr and isinstance(v, dict) and "usd" in v:
                    val = v["usd"]
                    break
        return float(val) if val is not None else None
    except Exception as e:
        print("CoinGecko contract price error:", e)
        return None

def _price_from_coingecko_cro():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        ids = "cronos,crypto-com-chain"
        r = SESSION.get(url, params={"ids": ids, "vs_currencies": "usd"}, timeout=8)
        data = safe_json(r)
        if not isinstance(data, dict):
            return None
        for key in ("cronos", "crypto-com-chain"):
            if key in data and isinstance(data[key], dict) and "usd" in data[key]:
                return float(data[key]["usd"])
        return None
    except Exception as e:
        print("CoinGecko CRO price error:", e)
        return None

def get_price_usd(symbol_or_addr: str):
    """Return float USD price for symbol or 0x-address. 60s cache. CRO-specific path.
    This NEVER merges tCRO with CRO (symbol-only path uses string, addr path uses contract).
    """
    if not symbol_or_addr:
        return None
    key = symbol_or_addr.strip().lower()

    # cache
    now_ts = time.time()
    cached = PRICE_CACHE.get(key)
    if cached and (now_ts - cached[1] < PRICE_CACHE_TTL):
        return cached[0]

    price = None
    try:
        if key in ("cro", "wcro", "w-cro", "wrappedcro", "wrapped cro"):
            price = _price_from_dexscreener_search("cro usdt") or _price_from_dexscreener_search("wcro usdt")
            if not price:
                price = _price_from_coingecko_cro()
        elif key.startswith("0x") and len(key) == 42:
            price = _price_from_dexscreener_token(key) or _price_from_coingecko_contract(key) or _price_from_dexscreener_search(key)
        else:
            price = _price_from_dexscreener_search(key)
            if not price and len(key) <= 8:
                price = _price_from_dexscreener_search(f"{key} usdt")
    except Exception:
        price = None

    PRICE_CACHE[key] = (price, now_ts)
    if price is None:
        print(f"Price lookup failed for '{symbol_or_addr}'")
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
        if isinstance(data, dict) and str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
            return data["result"]
        print("Unexpected wallet response:", str(data)[:600])
        return []
    except Exception as e:
        print("Error fetching wallet txs:", e)
        return []

def fetch_latest_token_txs(limit=50):
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
        if isinstance(data, dict) and str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
            return data["result"]
        print("Unexpected token tx response:", str(data)[:600])
        return []
    except Exception as e:
        print("Error fetching token txs:", e)
        return []

# ========================= Ledger / PnL =========================

def _format_amount(a):
    try:
        a = float(a)
    except Exception:
        return str(a)
    if abs(a) >= 1:
        return f"{a:,.4f}"
    if abs(a) >= 0.0001:
        return f"{a:.6f}"
    return f"{a:.8f}"


def _append_ledger(entry: dict):
    with _day_ledger_lock:
        path = data_file_for_today()
        data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        data["entries"].append(entry)
        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
        data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0))
        write_json(path, data)


def _update_cost_basis(token_key: str, signed_amount: float, price_usd: float):
    """Avg-cost model. Returns realized PnL for this movement."""
    global _realized_pnl_today
    qty = _position_qty[token_key]
    cost = _position_cost[token_key]
    realized = 0.0
    if signed_amount > EPSILON:
        # buy
        buy_qty = signed_amount
        add_cost = buy_qty * (price_usd or 0.0)
        _position_qty[token_key] = qty + buy_qty
        _position_cost[token_key] = cost + add_cost
    elif signed_amount < -EPSILON:
        # sell
        sell_qty_req = -signed_amount
        if qty > EPSILON:
            sell_qty = min(sell_qty_req, qty)
            avg_cost = (cost / qty) if qty > EPSILON else (price_usd or 0.0)
            realized = (price_usd - avg_cost) * sell_qty
            _position_qty[token_key] = qty - sell_qty
            _position_cost[token_key] = max(0.0, cost - avg_cost * sell_qty)
    _realized_pnl_today += realized
    return realized


def _replay_today_cost_basis():
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
        realized = _update_cost_basis(key, amt, price)
        e["realized_pnl"] = realized
    try:
        total_real = sum(float(e.get("realized_pnl", 0.0)) for e in data.get("entries", []))
        data["realized_pnl"] = total_real
        write_json(path, data)
    except Exception:
        pass

# ========================= ATH tracker =========================

def _load_ath():
    global _ath
    obj = read_json(ATH_FILE, default={})
    if isinstance(obj, dict):
        _ath = obj

def _save_ath():
    write_json(ATH_FILE, _ath)


def _check_update_ath(token_key: str, symbol: str, price: float):
    if price is None or price <= 0:
        return
    rec = _ath.get(token_key) or {"ath": 0.0, "ts": None}
    if price > float(rec.get("ath", 0.0)):
        # update & alert
        rec["ath"] = float(price)
        rec["ts"] = now_dt().isoformat(timespec="seconds")
        _ath[token_key] = rec
        _save_ath()
        send_telegram(md(f"🏔️ ATH reached for {symbol}: ${price:.6f}"))

# ========================= TX handlers =========================

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

    frm = (tx.get("from") or "").lower()
    to  = (tx.get("to") or "").lower()
    ts  = int(tx.get("timeStamp") or 0)
    dt  = datetime.fromtimestamp(ts) if ts > 0 else now_dt()

    sign = 0
    if to == WALLET_ADDRESS:
        sign = +1
    elif frm == WALLET_ADDRESS:
        sign = -1
    if sign == 0 or abs(amount_cro) <= EPSILON:
        return

    price = get_price_usd("CRO") or 0.0
    usd_value = sign * amount_cro * price

    _token_balances["CRO"] += sign * amount_cro
    _token_meta["CRO"] = {"symbol": "CRO", "decimals": 18}

    realized = _update_cost_basis("CRO", sign * amount_cro, price)

    link = CRONOS_TX.format(txhash=h)
    msg = (
        "*Native TX* (" + ("IN" if sign>0 else "OUT") + ") CRO\n" +
        "Hash: " + link + "\n" +
        "Time: " + dt.strftime("%H:%M:%S") + "\n" +
        "Amount: " + f"{sign*amount_cro:.6f} CRO  (~$" + _format_amount(amount_cro*price) + ")"
    )
    send_telegram(md(msg))

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
    frm = (t.get("from") or "").lower()
    to  = (t.get("to") or "").lower()
    if WALLET_ADDRESS not in (frm, to):
        return

    token_addr = (t.get("contractAddress") or "").lower()
    symbol     = (t.get("tokenSymbol") or token_addr[:8])
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
    msg = (
        "*Token TX* (" + ("IN" if sign>0 else "OUT") + ") " + symbol + "\n" +
        "Hash: " + link + "\n" +
        "Time: " + dt.strftime("%H:%M:%S") + "\n" +
        "Amount: " + f"{sign*amount:.6f} {symbol}  (~$" + _format_amount(abs(amount*price)) + ")"
    )
    send_telegram(md(msg))

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

# ========================= Monitors =========================

def wallet_monitor_loop():
    global _seen_tx_hashes
    print("Wallet monitor starting… seeding recent native tx hashes")

    initial = fetch_latest_wallet_txs(limit=50)
    try:
        _seen_tx_hashes = set(tx.get("hash") for tx in initial if isinstance(tx, dict) and tx.get("hash"))
    except Exception:
        _seen_tx_hashes = set()

    _replay_today_cost_basis()
    _load_ath()

    try:
        send_telegram(md(f"🚀 Wallet monitor started for {WALLET_ADDRESS} (Cronos)"))
    except SystemExit:
        print("Telegram token error on startup. Exiting wallet monitor.")
        return

    last_tokentx_seen = set()
    while True:
        # Native txs
        txs = fetch_latest_wallet_txs(limit=25)
        if txs:
            for tx in reversed(txs):
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
            if len(last_tokentx_seen) > 500:
                last_tokentx_seen = set(list(last_tokentx_seen)[-300:])

        time.sleep(WALLET_POLL)

# -------- Dexscreener helpers --------

def slug(chain: str, pair_address: str) -> str:
    return f"{chain}/{pair_address}".lower()

def fetch_pair(slg: str):
    try:
        r = SESSION.get(f"{DEX_BASE_PAIRS}/{slg}", timeout=12)
        return safe_json(r)
    except Exception as e:
        print("Error fetching pair", slg, e)
        return None

def fetch_token_pairs(chain: str, token_address: str):
    try:
        r = SESSION.get(f"{DEX_BASE_TOKENS}/{chain}/{token_address}", timeout=12)
        data = safe_json(r)
        if isinstance(data, dict) and isinstance(data.get("pairs"), list):
            return data["pairs"]
        return []
    except Exception as e:
        print("Error fetching token", chain, token_address, e)
        return []

def fetch_search(query: str):
    try:
        r = SESSION.get(DEX_BASE_SEARCH, params={"q": query}, timeout=15)
        data = safe_json(r)
        if isinstance(data, dict) and isinstance(data.get("pairs"), list):
            return data["pairs"]
        return []
    except Exception as e:
        print("Error search dexscreener:", e)
        return []

def ensure_tracking_pair(chain: str, pair_address: str, meta: dict | None = None):
    s = slug(chain, pair_address)
    if s in _tracked_pairs:
        return
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
    send_telegram(md(f"🆕 Now monitoring pair: {title}\n{ds_link}"))

# -------- Dexscreener monitor (tracked pairs) --------

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
        print("No tracked pairs yet; waiting for discovery/seed…")
    else:
        send_telegram(md(f"🚀 Dexscreener monitor started for: {', '.join(sorted(_tracked_pairs))}"))

    while True:
        if not _tracked_pairs:
            time.sleep(DEX_POLL)
            continue

        for s in list(_tracked_pairs):
            data = fetch_pair(s)
            if not data:
                continue
            # normalize
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

            # vol h1
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
            if price_val and price_val > 0:
                update_price_history(s, price_val)
                spike_pct = detect_spike(s)
                if spike_pct is not None:
                    if MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 < MIN_VOLUME_FOR_ALERT:
                        pass
                    else:
                        send_telegram(md(
                            f"🚨 Spike on {symbol}: {spike_pct:.2f}% over last {len(_price_history[s])} samples\n"
                            f"Price: ${price_val:.6f} Vol1h: {vol_h1}"
                        ))
                        _price_history[s].clear()
                        _last_prices[s] = price_val

            # price move vs last
            prev = _last_prices.get(s)
            if prev is not None and price_val is not None and prev != 0:
                delta = (price_val - prev) / prev * 100.0
                if abs(delta) >= PRICE_MOVE_THRESHOLD:
                    send_telegram(md(
                        f"📈 Price move on {symbol}: {delta:.2f}%\n"
                        f"Price: ${price_val:.6f} (prev ${prev:.6f})"
                    ))
                    _last_prices[s] = price_val

            # lastTx detection
            last_tx = pair.get("lastTx") or {}
            last_tx_hash = last_tx.get("hash") if isinstance(last_tx, dict) else None
            if last_tx_hash:
                prev_tx = _last_pair_tx.get(s)
                if prev_tx != last_tx_hash:
                    _last_pair_tx[s] = last_tx_hash
                    send_telegram(md(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx_hash)}"))

        time.sleep(DEX_POLL)

# -------- Auto discovery --------

def discovery_loop():
    # seed from DEX_PAIRS
    seeds = [p.strip().lower() for p in (DEX_PAIRS or '').split(',') if p.strip()]
    for s in seeds:
        if s.startswith("cronos/"):
            ensure_tracking_pair("cronos", s.split("/",1)[1])

    # seed from TOKENS → top pair
    token_items = [t.strip().lower() for t in (TOKENS or '').split(',') if t.strip()]
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

    if not DISCOVER_ENABLED:
        print("Discovery disabled (DISCOVER_ENABLED=false).")
        return
    send_telegram(md("🧭 Dexscreener auto-discovery enabled (Cronos)."))

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
    """Return (total_usd, breakdown list, unrealized_pnl). Keeps tCRO distinct."""
    total = 0.0
    breakdown = []
    unrealized = 0.0

    # CRO
    cro_amt = max(0.0, _token_balances.get("CRO", 0.0))
    if cro_amt > EPSILON:
        cro_price = get_price_usd("CRO") or 0.0
        cro_val = cro_amt * cro_price
        total += cro_val
        breakdown.append({
            "token": "CRO", "token_addr": None, "amount": cro_amt,
            "price_usd": cro_price, "usd_value": cro_val
        })
        rem_qty = _position_qty.get("CRO", 0.0)
        rem_cost= _position_cost.get("CRO", 0.0)
        if rem_qty > EPSILON:
            unrealized += (cro_val - rem_cost)
        _check_update_ath("CRO", "CRO", cro_price)

    # Tokens (addr keyed)
    for addr, amt in list(_token_balances.items()):
        if addr == "CRO":
            continue
        amt = max(0.0, amt)
        if amt <= EPSILON:
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
        rem_qty = _position_qty.get(addr, 0.0)
        rem_cost= _position_cost.get(addr, 0.0)
        if rem_qty > EPSILON:
            unrealized += (val - rem_cost)
        _check_update_ath(addr, sym, price)

    return total, breakdown, unrealized


def sum_month_net_flows_and_realized():
    pref = month_prefix()
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
            pnl_line = ""
            try:
                rp = float(e.get("realized_pnl", 0.0))
                if abs(rp) > 1e-9:
                    pnl_line = f"  PnL: ${_format_amount(rp)}"
            except Exception:
                pass
            lines.append(f"• {tm} — {direction} {tok} {_format_amount(amt)}  (${_format_amount(usd)}){pnl_line}")
        if len(entries) > MAX_LINES:
            lines.append(f"_…and {len(entries)-MAX_LINES} earlier txs._")

    lines.append(f"\n*Net USD flow today:* ${_format_amount(net_flow)}")
    lines.append(f"*Realized PnL today:* ${_format_amount(realized_today)}")

    holdings_total, breakdown, unrealized = compute_holdings_usd()
    lines.append(f"*Holdings (MTM) now:* ${_format_amount(holdings_total)}")
    if breakdown:
        for b in breakdown[:10]:
            lines.append(f"  – {b['token']}: {_format_amount(b['amount'])} @ ${_format_amount(b['price_usd'])} = ${_format_amount(b['usd_value'])}")
        if len(breakdown) > 10:
            lines.append(f"  …and {len(breakdown)-10} more.")
    lines.append(f"*Unrealized PnL (open positions):* ${_format_amount(unrealized)}")

    month_flow, month_real = sum_month_net_flows_and_realized()
    lines.append(f"*Month Net Flow:* ${_format_amount(month_flow)}")
    lines.append(f"*Month Realized PnL:* ${_format_amount(month_real)}")

    return "\n".join(lines)


def intraday_report_loop():
    time.sleep(10)
    send_telegram(md("⏱ Intraday reporting enabled."))
    last_sent = 0.0
    while True:
        if time.time() - last_sent >= INTRADAY_HOURS * 3600:
            try:
                txt = build_day_report_text()
                send_telegram(md("🟡 *Intraday Update*\n" + txt))
            except Exception as e:
                print("Intraday report error:", e)
            last_sent = time.time()
        time.sleep(30)


def end_of_day_scheduler_loop():
    send_telegram(md(f"🕛 End-of-day scheduler active (at {EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ})."))
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
            send_telegram(md("🟢 *End of Day Report*\n" + txt))
        except Exception as e:
            print("EOD report error:", e)

# Wrapper to safeguard monitor thread

def monitor_tracked_pairs_loop_wrapper():
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
    t_wallet   = threading.Thread(target=wallet_monitor_loop, daemon=True)
    t_pairs    = threading.Thread(target=monitor_tracked_pairs_loop_wrapper, daemon=True)
    t_discover = threading.Thread(target=discovery_loop, daemon=True)
    t_intraday = threading.Thread(target=intraday_report_loop, daemon=True)
    t_eod      = threading.Thread(target=end_of_day_scheduler_loop, daemon=True)

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
