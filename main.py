#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Complete Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner
Auto-discovery, PnL (realized & unrealized), intraday/EOD reports, ATH tracking, swap reconciliation,
Alerts Monitor (dump/pump) και Short-term Guard μετά από buy, με mini-summary ανά trade.

Drop-in για Railway worker. Χρησιμοποιεί ENV variables. Χωρίς hardcoded secrets.
"""

import os
import sys
import time
import json
import signal
import threading
import logging
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone
import math
import random

import requests
from dotenv import load_dotenv

load_dotenv()

# ----------------------- Config / ENV -----------------------
def _env_float(key, default):
    v = os.getenv(key, "")
    if v is None or str(v).strip() == "":
        return float(default)
    try:
        return float(v)
    except Exception:
        return float(default)

def _env_int(key, default):
    v = os.getenv(key, "")
    if v is None or str(v).strip() == "":
        return int(default)
    try:
        return int(v)
    except Exception:
        return int(default)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")      # Etherscan Multichain (chainid=25)
CRONOSCAN_API      = os.getenv("CRONOSCAN_API")      # optional snapshot

# Optional seeding (still supported)
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")      # "cronos/0xPAIR1,cronos/0xPAIR2"
TOKENS             = os.getenv("TOKENS", "")         # "cronos/0xTokenA,cronos/0xTokenB"

# Poll/monitor settings
PRICE_MOVE_THRESHOLD = _env_float("PRICE_MOVE_THRESHOLD", 5.0)
WALLET_POLL          = _env_int("WALLET_POLL", 15)
DEX_POLL             = _env_int("DEX_POLL", 60)
PRICE_WINDOW         = _env_int("PRICE_WINDOW", 3)
SPIKE_THRESHOLD      = _env_float("SPIKE_THRESHOLD", 8.0)
MIN_VOLUME_FOR_ALERT = _env_float("MIN_VOLUME_FOR_ALERT", 0.0)

# Discovery
DISCOVER_ENABLED     = (os.getenv("DISCOVER_ENABLED", "true").strip().lower() in ("1","true","yes","on"))
DISCOVER_QUERY       = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT       = _env_int("DISCOVER_LIMIT", 10)
DISCOVER_POLL        = _env_int("DISCOVER_POLL", 120)

# Time / reports
TZ                   = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS       = _env_int("INTRADAY_HOURS", 3)
EOD_HOUR             = _env_int("EOD_HOUR", 23)
EOD_MINUTE           = _env_int("EOD_MINUTE", 59)

# -------- Discovery Filters (NEW) --------
DISCOVER_MIN_LIQ_USD       = _env_float("DISCOVER_MIN_LIQ_USD", 30000.0)  # $30k
DISCOVER_MIN_VOL24_USD     = _env_float("DISCOVER_MIN_VOL24_USD", 5000.0) # $5k
DISCOVER_MIN_ABS_PCT_1H    = _env_float("DISCOVER_MIN_ABS_PCT_1H", 10.0)  # 1h move >= 10% (abs)
DISCOVER_MIN_ABS_PCT_4H    = _env_float("DISCOVER_MIN_ABS_PCT_4H", 0.0)   # optional
DISCOVER_MIN_ABS_PCT_6H    = _env_float("DISCOVER_MIN_ABS_PCT_6H", 0.0)   # optional
DISCOVER_MAX_PAIR_AGE_HOURS= _env_int("DISCOVER_MAX_PAIR_AGE_HOURS", 24)  # only pairs created in last 24h
DISCOVER_REQUIRE_WCRO_QUOTE= (os.getenv("DISCOVER_REQUIRE_WCRO_QUOTE","false").lower() in ("1","true","yes","on"))
DISCOVER_BASE_WHITELIST    = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_WHITELIST","").split(",") if s.strip()]
DISCOVER_BASE_BLACKLIST    = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_BLACKLIST","").split(",") if s.strip()]

# Alerts Monitor (new)
ALERTS_INTERVAL_MIN          = _env_int("ALERTS_INTERVAL_MIN", 15)    # check every 15 minutes
DUMP_ALERT_24H_PCT           = _env_float("DUMP_ALERT_24H_PCT", -15)  # -15%
PUMP_ALERT_24H_PCT           = _env_float("PUMP_ALERT_24H_PCT", 20)   # +20%

# Risky lists and custom thresholds
# Example: "WAVE,MOON,MERY,FM,ALI"
RISKY_SYMBOLS                = [s.strip().upper() for s in os.getenv("RISKY_SYMBOLS","").split(",") if s.strip()]
# Example: "MERY/WCRO=-10;MOON/WCRO=-20"
RISKY_THRESHOLDS_RAW         = os.getenv("RISKY_THRESHOLDS","").strip()
# Risky pump override (optional)
RISKY_PUMP_THRESHOLD_RAW     = os.getenv("RISKY_PUMP_THRESHOLDS","").strip()

# --- Interactive / Guard / ATH (new) ---
GUARD_WINDOW_MIN             = _env_int("GUARD_WINDOW_MIN", 60)  # protect for 60 minutes after buy
GUARD_DROP_PCT               = _env_float("GUARD_DROP_PCT", -8)  # alert if drops below -8% from entry during guard window

# ----------------------- Constants -----------------------
ETHERSCAN_V2_URL  = "https://api.etherscan.io/v2/api"   # multichain endpoint
CRONOS_CHAINID    = 25

DEX_BASE_PAIRS    = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS   = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH   = "https://api.dexscreener.com/latest/dex/search"

CRONOS_TX         = "https://cronoscan.com/tx/{txhash}"
DEXSITE_PAIR      = "https://dexscreener.com/{chain}/{pair}"

TELEGRAM_URL      = "https://api.telegram.org/bot{token}/sendMessage"
DATA_DIR          = "/app/data"
ATH_FILE          = os.path.join(DATA_DIR, "ath.json")

# ----------------------- Logging -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | INFO | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wallet-monitor")

# ----------------------- HTTP session -----------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/124.0 Safari/537.36"})

# simple rate-limiting (best-effort)
_last_http_ts = 0.0
def safe_get(url, *, params=None, timeout=12, max_retries=3, backoff_base=0.7):
    """GET με μικρό rate-limit + retries σε 403/404/429/5xx."""
    global _last_http_ts
    for attempt in range(1, max_retries+1):
        # soft rate limit ~5 req/sec
        now = time.time()
        delta = now - _last_http_ts
        if delta < 0.20:
            time.sleep(0.20 - delta)
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            _last_http_ts = time.time()
            if r.status_code == 200:
                return r
            if r.status_code in (403,404,429) or 500 <= r.status_code < 600:
                sl = backoff_base * (2 ** (attempt-1)) + random.uniform(0, 0.15)
                log.debug(f"[safe_get] {r.status_code} {url} retry in {sl:.2f}s (attempt {attempt}/{max_retries})")
                time.sleep(sl)
                continue
            return r
        except Exception as e:
            sl = backoff_base * (2 ** (attempt-1)) + random.uniform(0, 0.25)
            log.debug(f"[safe_get] exception {e} retry in {sl:.2f}s (attempt {attempt}/{max_retries})")
            time.sleep(sl)
    return None

# ----------------------- Shutdown event -----------------------
shutdown_event = threading.Event()

# ----------------------- State -----------------------
_seen_tx_hashes    = set()
_last_prices       = {}               # slug -> last price
_price_history     = {}               # slug -> deque
_last_pair_tx      = {}               # slug -> last tx hash
_tracked_pairs     = set()            # "cronos/0xPAIR"
_known_pairs_meta  = {}               # meta by slug

# Ledger / PnL state
_day_ledger_lock   = threading.Lock()
_token_balances    = defaultdict(float)    # token_addr or "CRO" -> amount (runtime)
_token_meta        = {}                    # addr -> {"symbol","decimals"}
_position_qty      = defaultdict(float)    # for avg-cost
_position_cost     = defaultdict(float)
_realized_pnl_today= 0.0

# caches
PRICE_CACHE        = {}
PRICE_CACHE_TTL    = 60
EPSILON            = 1e-12
_last_intraday_sent= 0.0

# alerts state (cooldowns)
_last_alert_sent   = {}   # key -> ts (avoid spam)

# Ensure data dir & timezone
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass
try:
    os.environ["TZ"] = TZ
    if hasattr(time, "tzset"):
        time.tzset()
except Exception:
    pass

# ----------------------- Utils -----------------------
def now_dt():
    return datetime.now()

def ymd(dt=None):
    if dt is None: dt = now_dt()
    return dt.strftime("%Y-%m-%d")

def month_prefix(dt=None):
    if dt is None: dt = now_dt()
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

def _format_amount(a):
    if a is None: return "0"
    try: a = float(a)
    except Exception: return str(a)
    if abs(a) >= 1: return f"{a:,.4f}"
    if abs(a) >= 0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

def _format_price(p):
    try: p = float(p)
    except Exception: return str(p)
    return f"{p:,.6f}"

def _nonzero(v, eps=1e-12):
    try:
        return abs(float(v)) > eps
    except Exception:
        return False

def safe_json(r):
    if r is None: return None
    if not getattr(r, "ok", False):
        return None
    try:
        return r.json()
    except Exception:
        return None

def send_telegram(message: str) -> bool:
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            log.warning("Telegram not configured.")
            return False
        url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        r = safe_get(url, params=payload, timeout=12, max_retries=2)
        if r is None:
            return False
        if r.status_code == 401:
            log.error("Telegram 401 Unauthorized. Check token.")
            return False
        if r.status_code != 200:
            log.warning("Telegram API returned %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        log.exception("send_telegram exception: %s", e)
        return False

def cooldown(key: str, seconds: int) -> bool:
    """Return True if allowed to send now (i.e., cooldown passed), else False."""
    ts = _last_alert_sent.get(key, 0)
    now = time.time()
    if now - ts >= seconds:
        _last_alert_sent[key] = now
        return True
    return False

# ----------------------- ATH persistence -----------------------
_ath_lock = threading.Lock()
_ath_map  = {}  # token_key -> {"ath": float, "ts": iso}

def load_ath():
    global _ath_map
    data = read_json(ATH_FILE, default={})
    if isinstance(data, dict):
        _ath_map = data
    else:
        _ath_map = {}

def save_ath():
    with _ath_lock:
        write_json(ATH_FILE, _ath_map)

def update_ath(token_key: str, price: float) -> bool:
    """Return True if new ATH set."""
    if price is None or price <= 0: return False
    rec = _ath_map.get(token_key)
    if not rec or float(rec.get("ath", 0.0)) < price:
        _ath_map[token_key] = {"ath": float(price), "ts": now_dt().isoformat()}
        save_ath()
        return True
    return False

# ----------------------- Price helpers -----------------------
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
        # try chain-specific
        url = f"{DEX_BASE_TOKENS}/cronos/{token_addr}"
        r = safe_get(url, timeout=10, max_retries=2)
        if r and r.status_code == 200:
            data = safe_json(r)
            p = _top_price_from_pairs_pricehelpers((data or {}).get("pairs"))
            if p: return p
        # fallback generic tokens/{addr}
        url2 = f"{DEX_BASE_TOKENS}/{token_addr}"
        r2 = safe_get(url2, timeout=10, max_retries=2)
        if r2 and r2.status_code == 200:
            data2 = safe_json(r2)
            p2 = _top_price_from_pairs_pricehelpers((data2 or {}).get("pairs"))
            if p2: return p2
        return None
    except Exception:
        return None

def _price_from_dexscreener_search(symbol_or_query):
    try:
        r = safe_get(DEX_BASE_SEARCH, params={"q": symbol_or_query}, timeout=12, max_retries=2)
        data = safe_json(r)
        return _top_price_from_pairs_pricehelpers((data or {}).get("pairs"))
    except Exception:
        return None

def _price_from_coingecko_contract(token_addr):
    try:
        addr = token_addr.lower()
        url = "https://api.coingecko.com/api/v3/simple/token_price/cronos"
        params = {"contract_addresses": addr, "vs_currencies": "usd"}
        r = safe_get(url, params=params, timeout=12, max_retries=2)
        data = safe_json(r)
        if not data: return None
        v = data.get(addr)
        if v and "usd" in v:
            return float(v["usd"])
    except Exception:
        pass
    return None

def _price_from_coingecko_ids_for_cro():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        ids = "cronos,crypto-com-chain"
        r = safe_get(url, params={"ids": ids, "vs_currencies": "usd"}, timeout=8, max_retries=2)
        data = safe_json(r)
        if not data: return None
        for idk in ("cronos", "crypto-com-chain"):
            if idk in data and "usd" in data[idk]:
                return float(data[idk]["usd"])
    except Exception:
        pass
    return None

def get_price_usd(symbol_or_addr: str):
    """Robust price fetch (cached). Accepts CRO or ERC20 contract (0x...)."""
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
    if key in ("cro", "wcro", "w-cro", "wrappedcro", "wrapped cro"):
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

    PRICE_CACHE[key] = (price, now_ts)
    return price

def get_token_price(chain: str, token_address: str) -> float:
    """Token price with several fallbacks. Returns 0.0 if not found."""
    try:
        # 1) /tokens/{chain}/{addr}
        url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
        r = safe_get(url, timeout=10, max_retries=2)
        if r and r.status_code == 200:
            d = safe_json(r)
            p = _top_price_from_pairs_pricehelpers((d or {}).get("pairs"))
            if p:
                return float(p)

        # 2) /tokens/{addr}
        url2 = f"{DEX_BASE_TOKENS}/{token_address}"
        r2 = safe_get(url2, timeout=10, max_retries=2)
        if r2 and r2.status_code == 200:
            d2 = safe_json(r2)
            p2 = _top_price_from_pairs_pricehelpers((d2 or {}).get("pairs"))
            if p2:
                return float(p2)

        # 3) search
        p3 = _price_from_dexscreener_search(token_address)
        if p3:
            return float(p3)

        # 4) coingecko fallback
        cg = _price_from_coingecko_contract(token_address)
        if cg:
            return float(cg)

        return 0.0
    except Exception:
        return 0.0

# ----------------------- Etherscan fetchers -----------------------
def fetch_latest_wallet_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API:
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
    r = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, max_retries=2)
    data = safe_json(r)
    if not data:
        return []
    if str(data.get("status","")).strip() == "1" and isinstance(data.get("result"), list):
        return data["result"]
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
    r = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, max_retries=2)
    data = safe_json(r)
    if not data:
        return []
    if str(data.get("status","")).strip() == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

# ----------------------- Ledger helpers -----------------------
def _append_ledger(entry: dict):
    with _day_ledger_lock:
        path = data_file_for_today()
        data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        data["entries"].append(entry)
        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
        data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0))
        write_json(path, data)

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

# ----------------------- Cost-basis / PnL -----------------------
def _update_cost_basis(token_key: str, signed_amount: float, price_usd: float):
    """Avg-cost model. Positive -> buy. Negative -> sell (realize PnL)."""
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

# ----------------------- Handlers (native + erc20) -----------------------
# guard tracker: track recent buys per token to guard against short-term dump
_guard_lock = threading.Lock()
_recent_buys = {}  # token_key -> list of {"ts": epoch, "price": float, "amount": float}

def _track_buy_guard(token_key: str, price: float, amount: float):
    if price is None or price <= 0 or amount is None or amount <= 0:
        return
    with _guard_lock:
        L = _recent_buys.get(token_key, [])
        L.append({"ts": time.time(), "price": float(price), "amount": float(amount)})
        # keep last 100
        if len(L) > 100:
            L = L[-100:]
        _recent_buys[token_key] = L

def _mini_inline_summary(e: dict):
    """Small one-line summary per tx (used inside Telegram logs)."""
    tok = e.get("token") or "?"
    direction = "IN" if float(e.get("amount") or 0) > 0 else "OUT"
    unit_price = e.get("price_usd") or 0.0
    usd = e.get("usd_value") or 0.0
    rp = float(e.get("realized_pnl", 0.0))
    pnl_line = f"  PnL: ${_format_amount(rp)}" if abs(rp) > 1e-9 else ""
    return f"{direction} {tok} {_format_amount(e.get('amount'))} @ ${_format_price(unit_price)} (${_format_amount(usd)}){pnl_line}"

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
    if to == WALLET_ADDRESS: sign = +1
    elif frm == WALLET_ADDRESS: sign = -1
    if sign == 0 or abs(amount_cro) <= EPSILON:
        return

    price = get_price_usd("CRO") or 0.0
    usd_value = sign * amount_cro * price

    _token_balances["CRO"] += sign * amount_cro
    _token_meta["CRO"] = {"symbol": "CRO", "decimals": 18}

    realized = _update_cost_basis("CRO", sign * amount_cro, price)

    # ATH update
    if price and update_ath("CRO", price) and cooldown("ath_CRO", 900):
        send_telegram(f"🏆 *New ATH* CRO: ${_format_price(price)}")

    # guard (for buys)
    if sign > 0 and amount_cro > 0:
        _track_buy_guard("CRO", price, amount_cro)

    link = CRONOS_TX.format(txhash=h)
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
    send_telegram(
        "*Native TX* " + _mini_inline_summary(entry) + f"\nHash: {link}\nTime: {dt.strftime('%H:%M:%S')}"
    )

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
    # prefer contract-based price
    price = 0.0
    if token_addr and token_addr.startswith("0x") and len(token_addr) == 42:
        price = get_token_price("cronos", token_addr) or 0.0
        if not price:
            price = get_price_usd(token_addr) or 0.0
    else:
        price = get_price_usd(symbol) or 0.0
    usd_value = sign * amount * price

    _token_balances[token_addr] += sign * amount
    _token_meta[token_addr] = {"symbol": symbol, "decimals": decimals}

    realized = _update_cost_basis(token_addr, sign * amount, price)

    # ATH update (αν έχεις helper, κράτα το· αλλιώς άστο έτσι)
    # try:
    #     update_ath(token_addr or symbol, price)
    # except Exception as _e:
    #     log.debug("ATH update error: %s", _e)

    # Telegram για το ίδιο το trade
    link = CRONOS_TX.format(txhash=h)
    try:
        send_telegram(
            f"Token TX {'IN' if sign>0 else 'OUT'} {symbol} "
            f"{_format_amount(sign*amount)} @ ${_format_price(price)} "
            f"(${_format_amount(usd_value)})\n"
            f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}"
        )
    except Exception as _e:
        log.debug("token-tx telegram error: %s", _e)

    # Καταγραφή στο ημερήσιο ledger
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

    # --- Mini inline summary (immediate feedback) ---
    try:
        per = summarize_today_per_asset()  # πρέπει ήδη να υπάρχει στο main.py σου
        tok_sym = symbol.upper()
        rec = per.get(tok_sym)
        if rec:
            flow      = rec.get("net_flow_today", 0.0)
            real      = rec.get("realized_today", 0.0)
            qty_today = rec.get("net_qty_today", 0.0)
            price_now = rec.get("price_now", 0.0)
            unreal    = rec.get("unreal_now", 0.0)

            line = (
                f"📊 *{tok_sym} intraday* | flow ${_format_amount(flow)}"
                f" | realized ${_format_amount(real)}"
            )
            if abs(qty_today) > EPSILON:
                line += f" | qty {_format_amount(qty_today)} @ ${_format_price(price_now)}"
            if price_now and abs(unreal) > EPSILON:
                line += f" | unreal ${_format_amount(unreal)}"

            send_telegram(line)
    except Exception as _e:
        log.debug("mini-summary error: %s", _e)

    # ATH update
    key = token_addr or symbol
    if price and update_ath(key, price) and cooldown(f"ath_{key}", 900):
        send_telegram(f"🏆 *New ATH* {symbol}: ${_format_price(price)}")

    # guard (for buys)
    if sign > 0 and amount > 0:
        _track_buy_guard(key, price, amount)

    link = CRONOS_TX.format(txhash=h)
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
    send_telegram(
        "*Token TX* " + _mini_inline_summary(entry) + f"\nHash: {link}\nTime: {dt.strftime('%H:%M:%S')}"
    )

# ----------------------- Wallet monitor loop -----------------------
def wallet_monitor_loop():
    """
    Main wallet monitor: polls native txs and token txs and processes them.
    """
    global _seen_tx_hashes
    log.info("Wallet monitor starting; loading initial recent txs...")

    initial = fetch_latest_wallet_txs(limit=50)
    try:
        _seen_tx_hashes = set(tx.get("hash") for tx in initial if isinstance(tx, dict) and tx.get("hash"))
    except Exception:
        _seen_tx_hashes = set()

    _replay_today_cost_basis()

    try:
        send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}` (Cronos).")
    except Exception:
        pass

    last_tokentx_seen = set()
    while not shutdown_event.is_set():
        # native txs
        try:
            txs = fetch_latest_wallet_txs(limit=25)
            if txs:
                for tx in reversed(txs):  # oldest -> newest
                    if not isinstance(tx, dict):
                        continue
                    handle_native_tx(tx)
        except Exception as e:
            log.exception("wallet native tx loop error: %s", e)

        # erc20 transfers
        try:
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
        except Exception as e:
            log.exception("wallet token tx loop error: %s", e)

        # sleep
        for _ in range(WALLET_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ----------------------- Dexscreener pair monitor + discovery -----------------------
def slug(chain: str, pair_address: str) -> str:
    return f"{chain}/{pair_address}".lower()

def fetch_pair(slug_str: str):
    url = f"{DEX_BASE_PAIRS}/{slug_str}"
    r = safe_get(url, timeout=12, max_retries=2)
    return safe_json(r)

def fetch_token_pairs(chain: str, token_address: str):
    url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
    r = safe_get(url, timeout=12, max_retries=2)
    data = safe_json(r)
    if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
        return data["pairs"]
    return []

def fetch_search(query: str):
    r = safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=15, max_retries=2)
    data = safe_json(r)
    if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
        return data["pairs"]
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
        log.info("No tracked pairs; monitor waits until discovery/seed adds some.")
    else:
        send_telegram(f"🚀 Dexscreener monitor started for: {', '.join(sorted(_tracked_pairs))}")

    while not shutdown_event.is_set():
        if not _tracked_pairs:
            time.sleep(DEX_POLL)
            continue
        for s in list(_tracked_pairs):
            try:
                data = fetch_pair(s)
                if not data:
                    continue
                pair = None
                if isinstance(data, dict) and isinstance(data.get("pair"), dict):
                    pair = data["pair"]
                elif isinstance(data, dict) and isinstance(data.get("pairs"), list) and data["pairs"]:
                    pair = data["pairs"][0]
                else:
                    continue

                price_val = None
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

                # history + spike
                if price_val is not None and price_val > 0:
                    update_price_history(s, price_val)
                    spike_pct = detect_spike(s)
                    if spike_pct is not None:
                        if MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 < MIN_VOLUME_FOR_ALERT:
                            pass
                        else:
                            if cooldown(f"spike_{s}", 180):
                                send_telegram(
                                    f"🚨 Spike on {symbol}: {spike_pct:.2f}% over recent samples\n"
                                    f"Price: ${price_val:.6f}"
                                )
                                _price_history[s].clear()
                                _last_prices[s] = price_val

                # price move vs last
                prev = _last_prices.get(s)
                if prev is not None and price_val is not None and prev != 0:
                    delta = (price_val - prev) / prev * 100.0
                    if abs(delta) >= PRICE_MOVE_THRESHOLD and cooldown(f"move_{s}", 180):
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
                    if prev_tx != last_tx_hash and cooldown(f"pairtx_{s}", 120):
                        _last_pair_tx[s] = last_tx_hash
                        send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx_hash)}")
            except Exception as e:
                log.debug("monitor_tracked_pairs_loop error for %s: %s", s, e)

        for _ in range(DEX_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ------------- END OF PART 1/2 -------------
# ----------------------- Discovery with filters -----------------------
def _pair_passes_filters(p: dict) -> (bool, str):
    """
    Επιστρέφει (True, "") αν περνάει τα φίλτρα μας, αλλιώς (False, reason).
    Φίλτρα: chain=cronos, WCRO quote (optional), whitelist/blacklist base, min liq, min vol24,
    abs change thresholds, max age hours.
    """
    try:
        if str(p.get("chainId","")).lower() != "cronos":
            return False, "not cronos"

        base = (p.get("baseToken") or {})
        quote = (p.get("quoteToken") or {})
        base_sym  = (base.get("symbol")  or "").upper()
        quote_sym = (quote.get("symbol") or "").upper()

        if DISCOVER_REQUIRE_WCRO_QUOTE and quote_sym not in ("WCRO","CRO"):
            return False, "quote not WCRO/CRO"

        if DISCOVER_BASE_WHITELIST and base_sym not in DISCOVER_BASE_WHITELIST:
            return False, "base not in whitelist"
        if base_sym in DISCOVER_BASE_BLACKLIST:
            return False, "base in blacklist"

        liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq_usd < DISCOVER_MIN_LIQ_USD:
            return False, f"liq<{DISCOVER_MIN_LIQ_USD}"

        vol24 = float((p.get("volume") or {}).get("h24") or 0)
        if vol24 < DISCOVER_MIN_VOL24_USD:
            return False, f"vol24<{DISCOVER_MIN_VOL24_USD}"

        # % changes (abs)
        def _abs_ok(field, min_abs):
            try:
                v = float(p.get(field) or 0)
                return abs(v) >= min_abs
            except Exception:
                return (min_abs <= 0)

        if DISCOVER_MIN_ABS_PCT_1H > 0 and not _abs_ok("priceChange.h1", DISCOVER_MIN_ABS_PCT_1H):
            return False, "abs 1h<th"
        if DISCOVER_MIN_ABS_PCT_4H > 0 and not _abs_ok("priceChange.h4", DISCOVER_MIN_ABS_PCT_4H):
            return False, "abs 4h<th"
        if DISCOVER_MIN_ABS_PCT_6H > 0 and not _abs_ok("priceChange.h6", DISCOVER_MIN_ABS_PCT_6H):
            return False, "abs 6h<th"

        # age
        created_ms = p.get("pairCreatedAt")
        if created_ms:
            try:
                created_dt = datetime.fromtimestamp(int(created_ms)/1000.0)
                age_h = (now_dt() - created_dt).total_seconds() / 3600.0
                if age_h > DISCOVER_MAX_PAIR_AGE_HOURS:
                    return False, "age>max"
            except Exception:
                pass

        return True, ""
    except Exception:
        return False, "exception"

def discovery_loop():
    # 1) Προαιρετικό seeding από DEX_PAIRS
    seeds = [s.strip().lower() for s in (DEX_PAIRS or "").split(",") if s.strip()]
    for s in seeds:
        if s.startswith("cronos/"):
            ensure_tracking_pair("cronos", s.split("/",1)[1])

    # 2) Προαιρετικό seeding από TOKENS -> resolve top pair
    token_items = [t.strip().lower() for t in (TOKENS or "").split(",") if t.strip()]
    for t in token_items:
        if not t.startswith("cronos/"):
            continue
        _, token_addr = t.split("/", 1)
        pairs = fetch_token_pairs("cronos", token_addr)
        if pairs:
            p = pairs[0]
            ok, _ = _pair_passes_filters(p)
            if ok:
                pair_addr = p.get("pairAddress")
                if pair_addr:
                    ensure_tracking_pair("cronos", pair_addr, meta=p)

    if not DISCOVER_ENABLED:
        log.info("Discovery disabled.")
        return

    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos) with filters.")

    while not shutdown_event.is_set():
        try:
            found = fetch_search(DISCOVER_QUERY)
            adopted = 0
            for p in found or []:
                ok, reason = _pair_passes_filters(p)
                if not ok:
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
            log.debug("Discovery error: %s", e)

        for _ in range(DISCOVER_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ----------------------- Wallet snapshot (Cronoscan optional) -----------------------
def get_wallet_balances_snapshot(address=None):
    balances = {}
    addr = (address or WALLET_ADDRESS or "").lower()
    if CRONOSCAN_API:
        try:
            url = "https://api.cronoscan.com/api"
            params = {"module":"account","action":"tokenlist","address":addr,"apikey":CRONOSCAN_API}
            r = safe_get(url, params=params, timeout=10, max_retries=2)
            data = safe_json(r)
            if isinstance(data, dict) and isinstance(data.get("result"), list):
                for tok in data.get("result", []):
                    sym = tok.get("symbol") or tok.get("tokenSymbol") or tok.get("name") or ""
                    try:
                        dec = int(tok.get("decimals") or tok.get("tokenDecimal") or 18)
                    except Exception:
                        dec = 18
                    bal_raw = tok.get("balance") or tok.get("tokenBalance") or "0"
                    try:
                        amt = int(bal_raw) / (10 ** dec)
                    except Exception:
                        try:
                            amt = float(bal_raw)
                        except Exception:
                            amt = 0.0
                    if sym:
                        balances[sym] = balances.get(sym, 0.0) + amt
        except Exception:
            pass

    # Fallback to internal runtime balances
    if not balances:
        for k, v in list(_token_balances.items()):
            if k == "CRO":
                balances["CRO"] = balances.get("CRO", 0.0) + v
            else:
                meta = _token_meta.get(k, {})
                sym = meta.get("symbol") or k[:8]
                balances[sym] = balances.get(sym, 0.0) + v
    if "CRO" not in balances:
        balances["CRO"] = float(_token_balances.get("CRO", 0.0))
    return balances

# ----------------------- Compute holdings / MTM -----------------------
def compute_holdings_usd():
    total = 0.0
    breakdown = []
    unrealized = 0.0

    # CRO
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

    # Tokens
    for addr, amt in list(_token_balances.items()):
        if addr == "CRO":
            continue
        amt = max(0.0, amt)
        if amt <= EPSILON:
            continue
        meta = _token_meta.get(addr,{})
        sym = meta.get("symbol") or str(addr)[:8]
        price = 0.0
        if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
            price = get_token_price("cronos", addr) or get_price_usd(addr) or 0.0
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

# ----------------------- Month aggregates -----------------------
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

# ----------------------- Per-asset summarize (today, clean) -----------------------
def summarize_today_per_asset():
    """
    Επιστρέφει dict ανά token-symbol με:
      - net_flow_today (USD)
      - realized_today
      - net_qty_today (signed)
      - last_seen_price (από entries)
      - token_addr (αν υπάρχει)
      - price_now (live/fallback)
      - open_qty_now, unreal_now (μόνο αν open_qty_now>0 & price_now>0)
    Δεν εμφανίζουμε "αρνητικά holdings" (δηλ. net_qty_today<0). Το unreal βασίζεται στο open position (global avg-cost).
    """
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])

    agg = {}
    for e in entries:
        tok  = (e.get("token") or "?").upper()
        addr = e.get("token_addr")
        usd  = float(e.get("usd_value") or 0.0)
        qty  = float(e.get("amount") or 0.0)
        rp   = float(e.get("realized_pnl") or 0.0)
        prc  = float(e.get("price_usd") or 0.0)
        A = agg.get(tok, {"net_flow_today":0.0,"realized_today":0.0,"net_qty_today":0.0,"last_seen_price":0.0,"token_addr":None})
        A["net_flow_today"]   += usd
        A["realized_today"]   += rp
        A["net_qty_today"]    += qty
        if prc > 0:
            A["last_seen_price"]= prc
        if addr and not A["token_addr"]:
            A["token_addr"] = addr
        agg[tok] = A

    # second pass -> live price + unreal for open position (global)
    out = {}
    for tok, A in agg.items():
        addr = A.get("token_addr")
        # open qty now from global state
        key = addr if addr else ("CRO" if tok=="CRO" else tok)
        open_qty_now = _position_qty.get(key, 0.0)

        # live price
        price_now = 0.0
        if addr and isinstance(addr, str) and addr.startswith("0x") and len(addr)==42:
            price_now = get_token_price("cronos", addr) or 0.0
            if not price_now:
                price_now = get_price_usd(addr) or 0.0
        else:
            # CRO or symbol
            if tok == "CRO":
                price_now = get_price_usd("CRO") or 0.0
            else:
                price_now = get_price_usd(tok) or 0.0

        # fallback to last_seen_price if live missing
        if price_now <= 0 and A.get("last_seen_price",0) > 0:
            price_now = A["last_seen_price"]

        # unreal only for open >0 and price_now>0
        unreal_now = 0.0
        if open_qty_now > EPSILON and price_now > 0:
            current_val = open_qty_now * price_now
            rem_cost    = _position_cost.get(key, 0.0)
            unreal_now  = current_val - rem_cost

        out[tok] = {
            "net_flow_today": A["net_flow_today"],
            "realized_today": A["realized_today"],
            "net_qty_today":  A["net_qty_today"],
            "token_addr":     addr,
            "last_seen_price":A.get("last_seen_price",0.0),
            "price_now":      price_now,
            "open_qty_now":   open_qty_now if open_qty_now>EPSILON else 0.0,
            "unreal_now":     unreal_now if (open_qty_now>EPSILON and price_now>0) else 0.0,
        }
    return out

# ----------------------- Report builder (daily/intraday) -----------------------
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
        for e in entries[-MAX_LINES:]:
            tok = e.get("token") or "?"
            amt = e.get("amount") or 0
            usd = e.get("usd_value") or 0
            tm  = e.get("time","")[-8:]
            direction = "IN" if float(amt) > 0 else "OUT"
            unit_price = e.get("price_usd") or 0.0
            rp = float(e.get("realized_pnl",0.0))
            pnl_line = f"  PnL: ${_format_amount(rp)}" if abs(rp) > 1e-9 else ""
            lines.append(
                f"• {tm} — {direction} {tok} {_format_amount(amt)}  @ ${_format_price(unit_price)}  "
                f"(${_format_amount(usd)}){pnl_line}"
            )
        if len(entries) > MAX_LINES:
            lines.append(f"_…and {len(entries)-MAX_LINES} earlier txs._")

    lines.append(f"\n*Net USD flow today:* ${_format_amount(net_flow)}")
    lines.append(f"*Realized PnL today:* ${_format_amount(realized_today)}")

    holdings_total, breakdown, unrealized = compute_holdings_usd()
    lines.append(f"*Holdings (MTM) now:* ${_format_amount(holdings_total)}")
    if breakdown:
        for b in breakdown[:15]:
            tok = b['token']
            lines.append(
                f"  – {tok}: {_format_amount(b['amount'])} @ ${_format_price(b['price_usd'])} = ${_format_amount(b['usd_value'])}"
            )
        if len(breakdown) > 15:
            lines.append(f"  …and {len(breakdown)-15} more.")
    lines.append(f"*Unrealized PnL (open positions):* ${_format_amount(unrealized)}")

    # --- Per-Asset Summary (Today) with totals ---
    lines.append("\n*Per-Asset Summary (Today):*")
    per = summarize_today_per_asset()
    # ταξινόμηση με βάση το απόλυτο net_flow_today (μεγαλύτερα πρώτα)
    order = sorted(per.items(), key=lambda kv: abs(kv[1]["net_flow_today"]), reverse=True)
    for tok, rec in order[:20]:
        flow     = rec["net_flow_today"]
        real     = rec["realized_today"]
        qty_today= rec["net_qty_today"]
        price    = rec["price_now"]
        unreal   = rec["unreal_now"]
        # Γραμμή σύνοψης (σήμερα)
        line = f"  • {tok}: flow ${_format_amount(flow)} | realized ${_format_amount(real)}"
        # Αν είχαμε καθαρή προσθήκη σήμερα, δείξ’ την ποσότητα και την τιμή
        if abs(qty_today) > EPSILON:
            line += f" | today qty {_format_amount(qty_today)} | price ${_format_price(price)}"
        # Αν υπάρχει ανοιχτή θέση τώρα, δείξε και το unrealized
        if unreal and abs(unreal) > EPSILON:
            line += f" | unreal ${_format_amount(unreal)}"
        lines.append(line)
    if len(order) > 20:
        lines.append(f"  …and {len(order)-20} more.")

    month_flow, month_real = sum_month_net_flows_and_realized()
    lines.append(f"\n*Month Net Flow:* ${_format_amount(month_flow)}")
    lines.append(f"*Month Realized PnL:* ${_format_amount(month_real)}")
    return "\n".join(lines)

# ----------------------- Intraday & EOD reporters -----------------------
def intraday_report_loop():
    global _last_intraday_sent
    time.sleep(5)
    send_telegram("⏱ Intraday reporting enabled.")
    while not shutdown_event.is_set():
        try:
            if time.time() - _last_intraday_sent >= INTRADAY_HOURS * 3600:
                txt = build_day_report_text()
                send_telegram("🟡 *Intraday Update*\n" + txt)
                _last_intraday_sent = time.time()
        except Exception as e:
            log.exception("Intraday report error: %s", e)
        for _ in range(30):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def end_of_day_scheduler_loop():
    send_telegram(f"🕛 End-of-day scheduler active (at {EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ}).")
    while not shutdown_event.is_set():
        now = now_dt()
        target = now.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        wait_s = (target - now).total_seconds()
        while wait_s > 0 and not shutdown_event.is_set():
            s = min(wait_s, 30)
            time.sleep(s)
            wait_s -= s
        if shutdown_event.is_set():
            break
        try:
            txt = build_day_report_text()
            send_telegram("🟢 *End of Day Report*\n" + txt)
        except Exception as e:
            log.exception("EOD report error: %s", e)

# ----------------------- Reconciliation helper (basic pairing) -----------------------
def reconcile_swaps_from_entries():
    """
    Χονδρικό pairing OUT -> IN σε κοντινά entries (ίδιο tx ή κοντινή ώρα) για quick context.
    Επιστρέφει λίστα από tuples (sell_entry, buy_entry).
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

# ----------------------- Alerts Monitor (dump/pump 24h & risky) -----------------------
def _parse_thresholds(raw: str):
    """
    Παίρνει "MERY/WCRO=-10;MOON/WCRO=-20" => { "MERY/WCRO": -10.0, "MOON/WCRO": -20.0 }
    """
    out = {}
    if not raw: return out
    for part in raw.split(";"):
        part = part.strip()
        if not part: continue
        if "=" not in part: continue
        k, v = part.split("=", 1)
        k = k.strip().upper()
        try:
            out[k] = float(v.strip())
        except Exception:
            continue
    return out

RISKY_DUMP_THRESHOLDS = _parse_thresholds(RISKY_THRESHOLDS_RAW)
RISKY_PUMP_THRESHOLDS = _parse_thresholds(RISKY_PUMP_THRESHOLD_RAW)

def _change_fields(pair):
    pc = pair.get("priceChange") or {}
    # Dexscreener typical keys: "h24", "h6", "h4", "h1", "m5" etc.
    out = {}
    for k in ("h24","h6","h4","h1","m30","m15","m5"):
        try:
            out[k] = float(pc.get(k) or 0.0)
        except Exception:
            out[k] = 0.0
    return out

def alerts_monitor_loop():
    """
    1) Όλα τα coins που έχεις στο wallet => 24h dump/pump alerts ανά 15'
    2) "Risky" σύμβολα (από env ή πρόσφατες αγορές της ημέρας) => 2h (αν διατίθεται) & custom thresholds (per pair SYM/QUOTE)
    """
    send_telegram(f"🛰 Alerts monitor active every {ALERTS_INTERVAL_MIN}m. "
                  f"Wallet 24h dump/pump: {DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT}.")
    while not shutdown_event.is_set():
        try:
            # Build watchlists
            # all wallet coins
            wallet_bal = get_wallet_balances_snapshot()
            wallet_syms = [s for s,a in wallet_bal.items() if a and a > 0]

            # Risky = env symbols (we'll match by base symbol)
            risky_syms = set(RISKY_SYMBOLS)

            # recent buys from today's entries (base symbol from token_meta best effort)
            path = data_file_for_today()
            data = read_json(path, default={"entries":[]})
            for e in data.get("entries", []):
                if float(e.get("amount") or 0) > 0:
                    tok = (e.get("token") or "").upper()
                    if tok and tok not in ("CRO","WCRO"):
                        risky_syms.add(tok)

            # 1) All wallet coins -> 24h monitoring via token top pair % change
            for sym in wallet_syms:
                # find top pair for sym by search
                pairs = fetch_search(sym) or []
                # pick first cronos with base symbol match
                pair = None
                for p in pairs:
                    if str(p.get("chainId","")).lower() != "cronos":
                        continue
                    b = (p.get("baseToken") or {}).get("symbol","").upper()
                    if b == sym:
                        pair = p
                        break
                if not pair and pairs:
                    # fallback pick the first cronos at all
                    for p in pairs:
                        if str(p.get("chainId","")).lower() == "cronos":
                            pair = p
                            break
                if not pair:
                    continue

                ch = _change_fields(pair)
                base = (pair.get("baseToken") or {})
                quote= (pair.get("quoteToken") or {})
                base_sym  = (base.get("symbol")  or "").upper()
                quote_sym = (quote.get("symbol") or "").upper()
                pair_addr = pair.get("pairAddress")
                price     = float(pair.get("priceUsd") or 0.0)
                link      = DEXSITE_PAIR.format(chain="cronos", pair=pair_addr) if pair_addr else ""

                # dump
                if ch["h24"] <= DUMP_ALERT_24H_PCT and cooldown(f"dump24_{base_sym}", 900):
                    send_telegram(
                        f"📉 *Dump Alert* {base_sym}/{quote_sym} 24h {ch['h24']:.2f}%\n"
                        f"Price ${_format_price(price)}\n{link}"
                    )
                # pump
                if ch["h24"] >= PUMP_ALERT_24H_PCT and cooldown(f"pump24_{base_sym}", 900):
                    send_telegram(
                        f"🚀 *Pump Alert* {base_sym}/{quote_sym} 24h {ch['h24']:.2f}%\n"
                        f"Price ${_format_price(price)}\n{link}"
                    )

            # 2) Risky tokens -> try 2h if available, else 24h. Custom thresholds by "SYM/QUOTE"
            for sym in sorted(risky_syms):
                pairs = fetch_search(sym) or []
                # pick first Cronos
                pair = None
                for p in pairs:
                    if str(p.get("chainId","")).lower() == "cronos":
                        pair = p
                        break
                if not pair:
                    continue
                ch  = _change_fields(pair)
                base = (pair.get("baseToken") or {})
                quote= (pair.get("quoteToken") or {})
                base_sym  = (base.get("symbol")  or "").upper()
                quote_sym = (quote.get("symbol") or "").upper()
                pair_addr = pair.get("pairAddress")
                link      = DEXSITE_PAIR.format(chain="cronos", pair=pair_addr) if pair_addr else ""
                price     = float(pair.get("priceUsd") or 0.0)

                # prefer 2h if present, else 24h
                # Dexscreener δεν δίνει πάντα "h2", οπότε θα συνθέσουμε από h1/m30/m15 αν χρειαστεί (approx).
                change_window = ch.get("h1", 0.0)  # χρησιμοποιούμε h1 ως conservative short-term
                if change_window == 0.0:
                    change_window = ch.get("h24", 0.0)

                key_pair = f"{base_sym}/{quote_sym}".upper()
                dump_th  = RISKY_DUMP_THRESHOLDS.get(key_pair, DUMP_ALERT_24H_PCT)
                pump_th  = RISKY_PUMP_THRESHOLDS.get(key_pair, PUMP_ALERT_24H_PCT)

                # dump
                if change_window <= dump_th and cooldown(f"risky_dump_{key_pair}", 900):
                    send_telegram(
                        f"⚠️ *Risky Dump* {key_pair} {change_window:.2f}%\n"
                        f"Price ${_format_price(price)}\n{link}"
                    )

                # pump
                if change_window >= pump_th and cooldown(f"risky_pump_{key_pair}", 900):
                    send_telegram(
                        f"🔥 *Risky Pump* {key_pair} {change_window:.2f}%\n"
                        f"Price ${_format_price(price)}\n{link}"
                    )

        except Exception as e:
            log.exception("alerts_monitor error: %s", e)

        # sleep
        for _ in range(max(1, ALERTS_INTERVAL_MIN*60)):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ----------------------- Guard monitor (short-term drop after buy) -----------------------
def guard_monitor_loop():
    """
    Μετά από BUY, για GUARD_WINDOW_MIN λεπτά προστασία:
      Αν η live τιμή πέσει GUARD_DROP_PCT% από την τιμή εισόδου => Alert.
    Ελέγχουμε ανά ~60s.
    """
    send_telegram(f"🛡 Guard monitor active: {GUARD_WINDOW_MIN}m window, alert below {GUARD_DROP_PCT}% from entry.")
    while not shutdown_event.is_set():
        try:
            now_ts = time.time()
            with _guard_lock:
                # καθάρισε παλιές αγορές έξω από το window
                for key, L in list(_recent_buys.items()):
                    _recent_buys[key] = [x for x in L if (now_ts - x["ts"]) <= GUARD_WINDOW_MIN*60]
                    if not _recent_buys[key]:
                        del _recent_buys[key]

            # έλεγχος τιμών
            for key, L in list(_recent_buys.items()):
                # τρέχουσα τιμή
                price_now = 0.0
                if key == "CRO":
                    price_now = get_price_usd("CRO") or 0.0
                elif isinstance(key, str) and key.startswith("0x") and len(key)==42:
                    price_now = get_token_price("cronos", key) or get_price_usd(key) or 0.0
                else:
                    price_now = get_price_usd(key) or 0.0
                if price_now <= 0:
                    continue

                # για κάθε buy, έλεγξε drawdown
                for rec in L:
                    entry_price = rec["price"]
                    if entry_price and entry_price > 0:
                        change_pct = (price_now - entry_price) / entry_price * 100.0
                        if change_pct <= GUARD_DROP_PCT and cooldown(f"guard_{key}", 600):
                            send_telegram(
                                f"🛑 *Guard Alert* {key}: {change_pct:.2f}% vs entry ${_format_price(entry_price)} "
                                f"(now ${_format_price(price_now)})"
                            )
        except Exception as e:
            log.exception("guard_monitor error: %s", e)

        for _ in range(60):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ----------------------- Thread runner w/ restart -----------------------
def run_with_restart(fn, name, daemon=True):
    def runner():
        while not shutdown_event.is_set():
            try:
                log.info("Thread %s starting.", name)
                fn()
                log.info("Thread %s exited cleanly.", name)
                break
            except Exception as e:
                log.exception("Thread %s crashed: %s. Restarting in 3s...", name, e)
                for _ in range(3):
                    if shutdown_event.is_set():
                        break
                    time.sleep(1)
        log.info("Thread %s terminating.", name)
    t = threading.Thread(target=runner, daemon=daemon, name=name)
    t.start()
    return t

# ----------------------- Entrypoint -----------------------
def main():
    log.info("Starting monitor with config:")
    log.info("WALLET_ADDRESS: %s", WALLET_ADDRESS)
    log.info("TELEGRAM_BOT_TOKEN present: %s", bool(TELEGRAM_BOT_TOKEN))
    log.info("TELEGRAM_CHAT_ID: %s", TELEGRAM_CHAT_ID)
    log.info("ETHERSCAN_API present: %s", bool(ETHERSCAN_API))
    log.info("DEX_PAIRS: %s", DEX_PAIRS)
    log.info("DISCOVER_ENABLED: %s | DISCOVER_QUERY: %s", DISCOVER_ENABLED, DISCOVER_QUERY)
    log.info("TZ: %s | INTRADAY_HOURS: %s | EOD: %02d:%02d", TZ, INTRADAY_HOURS, EOD_HOUR, EOD_MINUTE)
    log.info("Alerts interval: %sm | Wallet 24h dump/pump: %s/%s | Risky defaults dump/pump: %s/%s",
             ALERTS_INTERVAL_MIN, DUMP_ALERT_24H_PCT, PUMP_ALERT_24H_PCT, DUMP_ALERT_24H_PCT, PUMP_ALERT_24H_PCT)

    # Load persisted ATHs
    load_ath()

    # Threads
    threads = []
    threads.append(run_with_restart(wallet_monitor_loop, "wallet_monitor"))
    threads.append(run_with_restart(monitor_tracked_pairs_loop, "pairs_monitor"))
    threads.append(run_with_restart(discovery_loop, "discovery"))
    threads.append(run_with_restart(intraday_report_loop, "intraday_report"))
    threads.append(run_with_restart(end_of_day_scheduler_loop, "eod_scheduler"))
    threads.append(run_with_restart(alerts_monitor_loop, "alerts_monitor"))
    threads.append(run_with_restart(guard_monitor_loop, "guard_monitor"))

    try:
        # main thread waits for shutdown_event
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt: shutting down...")
        shutdown_event.set()

    # wait short for threads to finish
    log.info("Waiting for threads to terminate...")
    for t in threading.enumerate():
        if t is threading.current_thread():
            continue
        t.join(timeout=2)
    log.info("Shutdown complete.")

# ----------------------- Signal handler -----------------------
def _signal_handler(sig, frame):
    log.info("Signal %s received, initiating shutdown...", sig)
    shutdown_event.set()

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

if __name__ == "__main__":
    main()
