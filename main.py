#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Complete Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner
Auto-discovery, PnL (realized & unrealized), intraday/EOD reports, ATH tracking, swap reconciliation,
and Alerts Monitor (dump/pump) for all wallet coins (24h) & "risky" pairs (2h/24h) with custom thresholds.

Drop-in for Railway worker. Uses environment variables (no hardcoded secrets).
"""

import os
import sys
import time
import json
import signal
import threading
import logging
from collections import deque, defaultdict
from datetime import datetime, timedelta
import math

import requests
from dotenv import load_dotenv

load_dotenv()

# ----------------------- Config / ENV -----------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")
CRONOSCAN_API      = os.getenv("CRONOSCAN_API")  # optional, for full snapshot

# Optional seeding (still supported, but not required)
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")  # not required; auto-discovery & wallet-driven tracking handle new pairs
TOKENS             = os.getenv("TOKENS", "")

# Poll/monitor settings
PRICE_MOVE_THRESHOLD    = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL             = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL                = int(os.getenv("DEX_POLL", "60"))
PRICE_WINDOW            = int(os.getenv("PRICE_WINDOW", "3"))
SPIKE_THRESHOLD         = float(os.getenv("SPIKE_THRESHOLD", "8.0"))
MIN_VOLUME_FOR_ALERT    = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

# Discovery
DISCOVER_ENABLED        = os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on")
DISCOVER_QUERY          = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT          = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL           = int(os.getenv("DISCOVER_POLL", "120"))

# Time / reports
TZ                      = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS          = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR                = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE              = int(os.getenv("EOD_MINUTE", "59"))

# Alerts Monitor (new)
ALERTS_INTERVAL_MINUTES = int(os.getenv("ALERTS_INTERVAL_MINUTES", "15"))
DUMP_ALERT_24H_PCT      = float(os.getenv("DUMP_ALERT_24H_PCT", "-15"))   # for all wallet coins
PUMP_ALERT_24H_PCT      = float(os.getenv("PUMP_ALERT_24H_PCT", "20"))    # for all wallet coins

# Risky lists and custom thresholds (by pair "SYMBOL/QUOTE" or by symbol)
# RISKY_SYMBOLS example: "WAVE,MOON,MERY,FM,ALI"
RISKY_SYMBOLS           = [s.strip() for s in os.getenv("RISKY_SYMBOLS", "").split(",") if s.strip()]
# RISKY_THRESHOLDS example: "MERY/WCRO=-10;MOON/WCRO=-20"
RISKY_THRESHOLDS_RAW    = os.getenv("RISKY_THRESHOLDS", "")
# Risky pump threshold (optional; defaults to PUMP_ALERT_24H_PCT if not set)
RISKY_PUMP_DEFAULT      = float(os.getenv("RISKY_PUMP_DEFAULT", str(PUMP_ALERT_24H_PCT)))
RISKY_DUMP_DEFAULT      = float(os.getenv("RISKY_DUMP_DEFAULT", "-15"))

# ----------------------- Constants -----------------------
ETHERSCAN_V2_URL   = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID     = 25

DEX_BASE_PAIRS     = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS    = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH    = "https://api.dexscreener.com/latest/dex/search"
CRONOS_TX          = "https://cronoscan.com/tx/{txhash}"
DEXSITE_PAIR       = "https://dexscreener.com/{chain}/{pair}"

TELEGRAM_URL       = "https://api.telegram.org/bot{token}/sendMessage"
DATA_DIR           = "/app/data"
ATH_FILE           = os.path.join(DATA_DIR, "ath.json")

# ----------------------- Logging -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wallet-monitor")

# ----------------------- HTTP session -----------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"})

# simple rate limiting for external APIs
_last_http_ts = 0.0
HTTP_RPS = 5.0  # max ~5 requests per second

def _rate_limit():
    global _last_http_ts
    now = time.time()
    min_interval = 1.0 / HTTP_RPS
    if now - _last_http_ts < min_interval:
        time.sleep(min_interval - (now - _last_http_ts))
    _last_http_ts = time.time()

def safe_get(url, params=None, timeout=12, max_retries=3, backoff=1.2):
    for attempt in range(max_retries):
        try:
            _rate_limit()
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return None
            # 404 → small backoff and retry once
            if r.status_code in (403, 404, 429, 500, 502, 503, 504):
                time.sleep(backoff * (attempt + 1))
                continue
            return None
        except Exception as e:
            log.debug("safe_get error %s (attempt %d/%d)", e, attempt+1, max_retries)
            time.sleep(backoff * (attempt + 1))
    return None

# ----------------------- Shutdown event -----------------------
shutdown_event = threading.Event()

# ----------------------- State -----------------------
_seen_tx_hashes   = set()
_last_prices      = {}
_price_history    = {}
_last_pair_tx     = {}
_tracked_pairs    = set()
_known_pairs_meta = {}

_day_ledger_lock  = threading.Lock()
_token_balances   = defaultdict(float)    # token_addr or "CRO" -> amount
_token_meta       = {}                    # token_addr -> {"symbol","decimals"}
_position_qty     = defaultdict(float)
_position_cost    = defaultdict(float)
_realized_pnl_today = 0.0
EPSILON           = 1e-12
_last_intraday_sent = 0.0

PRICE_CACHE = {}
PRICE_CACHE_TTL = 60

# alerts state (cooldowns)
_alerts_last_sent = {}  # key -> ts

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

def _format_price(p):
    try:
        p = float(p)
    except Exception:
        return str(p)
    return f"{p:,.6f}"

def _nonzero(v, eps=1e-12):
    try:
        return abs(float(v)) > eps
    except Exception:
        return False

def safe_json(r):
    if r is None:
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
        r = SESSION.post(url, data=payload, timeout=12)
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

# ----------------------- ATH persistence -----------------------
def load_ath():
    return read_json(ATH_FILE, {})

def save_ath(ath):
    write_json(ATH_FILE, ath)

# ----------------------- Price helpers -----------------------
def _top_price_from_pairs_pricehelpers(pairs):
    if not pairs:
        return None, None, None  # price, pairAddress, chainId
    best = None
    best_liq = -1.0
    best_addr = None
    best_chain = None
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
                best_addr = p.get("pairAddress")
                best_chain = chain_id or "cronos"
        except Exception:
            continue
    return best, best_addr, best_chain

def _price_from_dexscreener_token(token_addr):
    try:
        url = f"{DEX_BASE_TOKENS}/cronos/{token_addr}"
        d = safe_get(url, timeout=10)
        if not d:
            return None
        price, pair_addr, chain_id = _top_price_from_pairs_pricehelpers(d.get("pairs"))
        return price
    except Exception as e:
        log.debug("Error _price_from_dexscreener_token: %s", e)
        return None

def _search_best_pair(symbol_or_query):
    try:
        d = safe_get(DEX_BASE_SEARCH, params={"q": symbol_or_query}, timeout=12)
        if not d:
            return None
        pairs = d.get("pairs")
        if not isinstance(pairs, list):
            return None
        # prefer Cronos + quote in ('WCRO','CRO','USDT','USDC')
        preferred_quotes = ("WCRO","CRO","USDT","USDC","DAI")
        cronos_pairs = [p for p in pairs if str(p.get("chainId","")).lower()=="cronos"]
        if not cronos_pairs:
            cronos_pairs = pairs
        # pick by preferred quote then by liquidity
        best = None; best_liq=-1.0
        for p in cronos_pairs:
            try:
                quote = (p.get("quoteToken") or {}).get("symbol") or ""
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                if best is None or (quote in preferred_quotes and liq>best_liq) or (quote not in preferred_quotes and liq>best_liq):
                    best = p; best_liq = liq
            except Exception:
                continue
        return best
    except Exception as e:
        log.debug("Error _search_best_pair: %s", e)
        return None

def _price_from_dexscreener_search(symbol_or_query):
    p = _search_best_pair(symbol_or_query)
    if not p:
        return None
    try:
        return float(p.get("priceUsd") or 0) or None
    except Exception:
        return None

def _price_from_coingecko_contract(token_addr):
    try:
        addr = token_addr.lower()
        url = "https://api.coingecko.com/api/v3/simple/token_price/cronos"
        params = {"contract_addresses": addr, "vs_currencies": "usd"}
        d = safe_get(url, params=params, timeout=12)
        if not d:
            return None
        v = d.get(addr)
        if v and "usd" in v:
            return float(v["usd"])
    except Exception as e:
        log.debug("Error _price_from_coingecko_contract: %s", e)
    return None

def _price_from_coingecko_ids_for_cro():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        ids = "cronos,crypto-com-chain"
        d = safe_get(url, params={"ids": ids, "vs_currencies": "usd"}, timeout=8)
        if not d:
            return None
        for idk in ("cronos", "crypto-com-chain"):
            if idk in d and "usd" in d[idk]:
                return float(d[idk]["usd"])
    except Exception as e:
        log.debug("Error _price_from_coingecko_ids_for_cro: %s", e)
    return None

def get_price_usd(symbol_or_addr: str):
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
    if price is None:
        log.debug("Price lookup failed for '%s'", symbol_or_addr)
    return price

def get_token_price(chain: str, token_address: str):
    """
    Robust token price fetch: tries token pairs endpoint, tokens endpoint, search, coingecko fallback.
    Returns float price or 0.0 when not found.
    """
    try:
        url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
        d = safe_get(url, timeout=10)
        if d and isinstance(d, dict) and d.get("pairs"):
            p, _, _ = _top_price_from_pairs_pricehelpers(d["pairs"])
            if p:
                return float(p)

        url2 = f"{DEX_BASE_TOKENS}/{token_address}"
        d2 = safe_get(url2, timeout=10)
        if d2 and isinstance(d2, dict) and d2.get("pairs"):
            p, _, _ = _top_price_from_pairs_pricehelpers(d2["pairs"])
            if p:
                return float(p)

        p3 = _price_from_dexscreener_search(token_address)
        if p3:
            return float(p3)

        cg = _price_from_coingecko_contract(token_address)
        if cg:
            return float(cg)

        return 0.0
    except Exception as e:
        log.debug("[get_token_price] %s", e)
        return 0.0

# ----------------------- Etherscan fetchers -----------------------
def fetch_latest_wallet_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API:
        log.warning("Missing WALLET_ADDRESS or ETHERSCAN_API.")
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
        d = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15)
        if not d:
            return []
        if str(d.get("status","")).strip() == "1" and isinstance(d.get("result"), list):
            return d["result"]
        log.debug("Unexpected wallet response: %s", str(d)[:300])
        return []
    except Exception as e:
        log.debug("Error fetching wallet txs: %s", e)
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
        d = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15)
        if not d:
            return []
        if str(d.get("status","")).strip() == "1" and isinstance(d.get("result"), list):
            return d["result"]
        log.debug("Unexpected token tx response: %s", str(d)[:300])
        return []
    except Exception as e:
        log.debug("Error fetching token txs: %s", e)
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

    _token_balances["CRO"] += sign * amount_cro
    _token_meta["CRO"] = {"symbol": "CRO", "decimals": 18}

    realized = _update_cost_basis("CRO", sign * amount_cro, price)

    link = CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\n"
        f"Hash: {link}\n"
        f"Time: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount_cro:.6f} CRO\n"
        f"Price: ${_format_price(price)} per CRO\n"
        f"USD value: ${_format_amount(usd_value)}"
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

    # robust price: try by contract first
    price = get_token_price("cronos", token_addr) or get_price_usd(token_addr) or 0.0
    usd_value = sign * amount * price

    _token_balances[token_addr] += sign * amount
    _token_meta[token_addr] = {"symbol": symbol, "decimals": decimals}

    realized = _update_cost_basis(token_addr, sign * amount, price)

    link = CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Token TX* ({'IN' if sign>0 else 'OUT'}) {symbol}\n"
        f"Hash: {link}\n"
        f"Time: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {symbol}\n"
        f"Price: ${_format_price(price)} per {symbol}\n"
        f"USD value: ${_format_amount(usd_value)}"
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

# ----------------------- Wallet monitor loop -----------------------
def wallet_monitor_loop():
    """
    Main wallet monitor: polls native txs and token txs and processes them.
    """
    global _seen_tx_hashes
    log.info("Wallet monitor starting; loading initial recent txs...")
    # seed seen hashes to avoid spamming historic txs on startup
    initial = fetch_latest_wallet_txs(limit=50)
    try:
        _seen_tx_hashes = set(tx.get("hash") for tx in initial if isinstance(tx, dict) and tx.get("hash"))
    except Exception:
        _seen_tx_hashes = set()

    _replay_today_cost_basis()

    try:
        send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}` (Cronos).")
    except Exception:
        log.exception("Telegram startup notify failed.")

    last_tokentx_seen = set()
    while not shutdown_event.is_set():
        # native txs
        try:
            txs = fetch_latest_wallet_txs(limit=25)
            if not txs:
                log.debug("No native txs returned; retrying...")
            else:
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
    try:
        d = safe_get(url, timeout=12)
        return d
    except Exception as e:
        log.debug("Error fetching pair %s: %s", slug_str, e)
        return None

def fetch_token_pairs(chain: str, token_address: str):
    url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
    try:
        d = safe_get(url, timeout=12)
        if isinstance(d, dict) and "pairs" in d and isinstance(d["pairs"], list):
            return d["pairs"]
        return []
    except Exception as e:
        log.debug("Error fetching token %s %s: %s", chain, token_address, e)
        return []

def fetch_search(query: str):
    try:
        d = safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=15)
        if isinstance(d, dict) and "pairs" in d and isinstance(d["pairs"], list):
            return d["pairs"]
        return []
    except Exception as e:
        log.debug("Error search dexscreener: %s", e)
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
                    log.debug("Unexpected dexscreener format for %s", s)
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
                if price_val is not None and price_val > 0:
                    update_price_history(s, price_val)
                    spike_pct = detect_spike(s)
                    if spike_pct is not None:
                        if MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 < MIN_VOLUME_FOR_ALERT:
                            pass
                        else:
                            send_telegram(
                                f"🚨 Spike on {symbol}: {spike_pct:.2f}% over recent samples\n"
                                f"Price: ${price_val:.6f} Vol1h: {vol_h1}"
                            )
                            _price_history[s].clear()
                            _last_prices[s] = price_val
                prev = _last_prices.get(s)
                if prev is not None and price_val is not None and prev != 0:
                    delta = (price_val - prev) / prev * 100.0
                    if abs(delta) >= PRICE_MOVE_THRESHOLD:
                        send_telegram(
                            f"📈 Price move on {symbol}: {delta:.2f}%\n"
                            f"Price: ${price_val:.6f} (prev ${prev:.6f})"
                        )
                        _last_prices[s] = price_val
                last_tx = (pair.get("lastTx") or {}) if isinstance(pair, dict) else {}
                last_tx_hash = last_tx.get("hash") if isinstance(last_tx, dict) else None
                if last_tx_hash:
                    prev_tx = _last_pair_tx.get(s)
                    if prev_tx != last_tx_hash:
                        _last_pair_tx[s] = last_tx_hash
                        send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx_hash)}")
            except Exception as e:
                log.debug("monitor_tracked_pairs_loop error for %s: %s", s, e)
        for _ in range(DEX_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def discovery_loop():
    # seed from DEX_PAIRS env (optional)
    seeds = [p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    for s in seeds:
        if s.startswith("cronos/"):
            ensure_tracking_pair("cronos", s.split("/",1)[1])

    # seed from TOKENS env (optional)
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

    if not DISCOVER_ENABLED:
        log.info("Discovery disabled.")
        return

    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos).")

    while not shutdown_event.is_set():
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
            log.debug("Discovery error: %s", e)
        for _ in range(DISCOVER_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ----------------------- Wallet snapshot (Cronoscan optional) -----------------------
def get_wallet_balances_snapshot(address=None):
    balances = {}
    addr = (address or WALLET_ADDRESS or "").lower()
    # Try Cronoscan tokenlist
    if CRONOSCAN_API:
        try:
            url = "https://api.cronoscan.com/api"
            params = {"module":"account","action":"tokenlist","address":addr,"apikey":CRONOSCAN_API}
            d = safe_get(url, params=params, timeout=10)
            if isinstance(d, dict) and isinstance(d.get("result"), list):
                for tok in d.get("result", []):
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
        except Exception as e:
            log.debug("Cronoscan snapshot error: %s", e)

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
    """
    Returns (total_usd, breakdown list, unrealized_pnl)
    breakdown items: {"token","token_addr","amount","price_usd","usd_value"}
    - Shows only positive balances (no negative inventory lines).
    - Unrealized PnL = Σ(current_value - remaining_cost_basis) over tokens with position > 0.
    """
    total = 0.0
    breakdown = []
    unrealized = 0.0

    # CRO
    cro_amt = max(0.0, _token_balances.get("CRO", 0.0))  # clip negatives
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

    # Tokens
    for addr, amt in list(_token_balances.items()):
        if addr == "CRO":
            continue
        amt = max(0.0, amt)  # clip negative inventory
        if amt <= EPSILON:
            continue
        meta = _token_meta.get(addr, {})
        sym = meta.get("symbol") or addr[:8]
        price = 0.0
        if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
            price = get_token_price("cronos", addr) or get_price_usd(addr) or 0.0
        else:
            # fallback by symbol
            price = get_price_usd(sym) or 0.0
            if not price:
                # last try via search
                price = _price_from_dexscreener_search(sym) or 0.0
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

    # ATH tracking update (by token price), send alert on new ATH
    try:
        ath = load_ath()
        changed = False
        for b in breakdown:
            tok = b["token"]
            price = float(b.get("price_usd") or 0.0)
            if price <= 0:
                continue
            prev = float(ath.get(tok, 0.0))
            if price > prev:
                ath[tok] = price
                changed = True
                send_telegram(f"🏔 *New ATH price* for {tok}: ${_format_price(price)} (prev ${_format_price(prev)})")
        if changed:
            save_ath(ath)
    except Exception:
        pass

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

# ----------------------- Per-asset summarize (today) -----------------------
def summarize_today_per_asset():
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])

    per_asset_flow_usd = defaultdict(float)
    per_asset_real     = defaultdict(float)
    per_asset_qty      = defaultdict(float)
    per_asset_price    = {}          # last seen trade price
    per_asset_addr     = {}
    per_asset_is_open  = set()

    # aggregate
    for e in entries:
        tok  = e.get("token") or "?"
        addr = e.get("token_addr")
        amt  = float(e.get("amount") or 0.0)
        usd  = float(e.get("usd_value") or 0.0)
        px   = float(e.get("price_usd") or 0.0)
        rp   = float(e.get("realized_pnl") or 0.0)

        per_asset_flow_usd[tok] += usd
        per_asset_real[tok]     += rp
        per_asset_qty[tok]      += amt
        if _nonzero(px):
            per_asset_price[tok] = px
        if addr and tok not in per_asset_addr:
            per_asset_addr[tok] = addr

    # compute live price & unreal only for open qty > 0
    lines = []
    order = sorted(per_asset_flow_usd.items(), key=lambda kv: abs(kv[1]), reverse=True)
    LIMIT = 20
    for idx, (tok, flow) in enumerate(order[:LIMIT]):
        addr = per_asset_addr.get(tok)
        live_price = per_asset_price.get(tok)
        if live_price is None:
            if tok.upper() == "CRO":
                live_price = get_price_usd("CRO") or 0.0
            elif addr and isinstance(addr, str) and addr.startswith("0x"):
                live_price = get_token_price("cronos", addr) or 0.0
            else:
                live_price = get_price_usd(tok) or _price_from_dexscreener_search(tok) or 0.0
        qty_sum = per_asset_qty.get(tok, 0.0)
        real_sum = per_asset_real.get(tok, 0.0)

        # unrealized for open positive qty only
        unreal = 0.0
        if qty_sum > EPSILON:
            # approximate rem_cost from global positions if we have address or CRO
            key = "CRO" if tok.upper()=="CRO" else per_asset_addr.get(tok, tok)
            rem_qty = _position_qty.get(key, 0.0)
            rem_cost= _position_cost.get(key, 0.0)
            cur_val = float(live_price or 0.0) * float(rem_qty or 0.0)
            if rem_qty > EPSILON:
                unreal = cur_val - rem_cost

        lines.append(
            f"  • {tok}: flow ${_format_amount(flow)} | realized ${_format_amount(real_sum)} | "
            f"today qty {_format_amount(qty_sum)} | price ${_format_price(live_price)}"
            + (f" | unreal ${_format_amount(unreal)}" if qty_sum>EPSILON else "")
        )
    if len(order) > LIMIT:
        lines.append(f"  …and {len(order)-LIMIT} more.")
    return "\n".join(lines)

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
        MAX_LINES = 30
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
                f"• {tm} — {direction} {tok} {_format_amount(amt)}  "
                f"@ ${_format_price(unit_price)}  "
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

    # Per-asset summary (today)
    lines.append("\n*Per-Asset Summary (Today):*")
    lines.append(summarize_today_per_asset())

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

# ----------------------- Alerts Monitor (dump/pump) -----------------------
def _parse_risky_thresholds(raw: str):
    """
    Parses env RISKY_THRESHOLDS like: "MERY/WCRO=-10;MOON/WCRO=-20"
    Returns dict: {"MERY/WCRO": -10.0, "MOON/WCRO": -20.0}
    """
    out = {}
    if not raw:
        return out
    parts = [p.strip() for p in raw.split(";") if p.strip()]
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            k = k.strip()
            try:
                out[k] = float(v.strip())
            except Exception:
                continue
    return out

RISKY_THRESHOLDS = _parse_risky_thresholds(RISKY_THRESHOLDS_RAW)

def _symbol_balances():
    """
    Aggregates runtime balances by symbol to estimate exposure for alerts.
    """
    agg = defaultdict(float)
    for addr, amt in _token_balances.items():
        if addr == "CRO":
            agg["CRO"] += amt
        else:
            meta = _token_meta.get(addr, {})
            sym = meta.get("symbol") or addr[:8]
            agg[sym] += amt
    return dict(agg)

def _asset_market_info_by_symbol(symbol: str):
    """
    Returns dict with:
    - price_usd
    - change_h24
    - change_h2 (if available)
    - pairAddress
    - chainId
    - quoteSymbol
    """
    p = _search_best_pair(symbol)
    if not p:
        return None
    try:
        price = float(p.get("priceUsd") or 0.0)
    except Exception:
        price = 0.0
    change = p.get("priceChange") or {}
    def _tofloat(x):
        try:
            return float(x)
        except Exception:
            return None
    info = {
        "price_usd": price,
        "change_h24": _tofloat(change.get("h24")),
        "change_h2": _tofloat(change.get("h2")),
        "pairAddress": p.get("pairAddress"),
        "chainId": str(p.get("chainId") or "cronos").lower(),
        "quoteSymbol": ((p.get("quoteToken") or {}).get("symbol") or "").upper(),
        "baseSymbol": ((p.get("baseToken") or {}).get("symbol") or "").upper(),
    }
    return info

def _cooldown_ok(key: str, minutes: int = 60):
    last = _alerts_last_sent.get(key, 0)
    if time.time() - last >= minutes * 60:
        _alerts_last_sent[key] = time.time()
        return True
    return False

def alerts_monitor_loop():
    interval = max(1, ALERTS_INTERVAL_MINUTES) * 60
    send_telegram(f"🔔 Alerts monitor enabled (interval {ALERTS_INTERVAL_MINUTES}m).")
    while not shutdown_event.is_set():
        try:
            # Build watchlists
            sym_bal = _symbol_balances()
            wallet_symbols = set(sym_bal.keys())  # all wallet coins
            # Risky = env + recent buys today
            risky = set([s.upper() for s in RISKY_SYMBOLS])
            # recent buys from today's entries
            today = read_json(data_file_for_today(), {"entries":[]})
            for e in today.get("entries", []):
                try:
                    if float(e.get("amount") or 0) > 0:
                        risky.add((e.get("token") or "").upper())
                except Exception:
                    continue

            # 1) All wallet coins -> 24h monitoring
            for sym in sorted(wallet_symbols):
                info = _asset_market_info_by_symbol(sym)
                if not info:
                    continue
                ch24 = info.get("change_h24")
                price = float(info.get("price_usd") or 0.0)
                qty   = float(sym_bal.get(sym, 0.0))
                value = price * qty
                pair  = info.get("pairAddress")
                chain = info.get("chainId") or "cronos"
                link  = DEXSITE_PAIR.format(chain=chain, pair=pair) if pair else ""

                # dump alert
                if ch24 is not None and ch24 <= DUMP_ALERT_24H_PCT:
                    key = f"{sym}:24h_dump"
                    if _cooldown_ok(key, minutes=60):
                        send_telegram(
                            f"⚠️ *DUMP ALERT (24h)*: {sym} {ch24:.2f}%\n"
                            f"Price: ${_format_price(price)} | Qty: {_format_amount(qty)} | Value: ${_format_amount(value)}\n"
                            f"{link}"
                        )
                # pump alert
                if ch24 is not None and ch24 >= PUMP_ALERT_24H_PCT:
                    key = f"{sym}:24h_pump"
                    if _cooldown_ok(key, minutes=60):
                        send_telegram(
                            f"🚀 *PUMP ALERT (24h)*: {sym} +{ch24:.2f}%\n"
                            f"Price: ${_format_price(price)} | Qty: {_format_amount(qty)} | Value: ${_format_amount(value)}\n"
                            f"{link}"
                        )

            # 2) Risky tokens -> 2h (if available) and custom thresholds per pair symbol "SYM/QUOTE"
            for sym in sorted(risky):
                info = _asset_market_info_by_symbol(sym)
                if not info:
                    continue
                base = info.get("baseSymbol") or sym
                quote= info.get("quoteSymbol") or "WCRO"
                pair_sym = f"{base}/{quote}"
                custom_dump = RISKY_THRESHOLDS.get(pair_sym, RISKY_DUMP_DEFAULT)
                custom_pump = RISKY_PUMP_DEFAULT

                ch2  = info.get("change_h2")
                ch24 = info.get("change_h24")
                price= float(info.get("price_usd") or 0.0)
                qty  = float(sym_bal.get(base, 0.0))
                value= price * qty
                pair = info.get("pairAddress")
                chain= info.get("chainId") or "cronos"
                link = DEXSITE_PAIR.format(chain=chain, pair=pair) if pair else ""

                # prefer 2h if present, otherwise fall back to 24h for risky as well
                change_for_risky = ch2 if ch2 is not None else ch24

                if change_for_risky is None:
                    continue

                # dump
                if change_for_risky <= custom_dump:
                    key = f"{pair_sym}:risky_dump"
                    if _cooldown_ok(key, minutes=45):
                        scope = "2h" if ch2 is not None else "24h"
                        send_telegram(
                            f"⚠️ *RISKY DUMP* {scope}: {pair_sym} {change_for_risky:.2f}%\n"
                            f"Price: ${_format_price(price)} | Qty: {_format_amount(qty)} | Value: ${_format_amount(value)}\n"
                            f"{link}"
                        )
                # pump
                if change_for_risky >= custom_pump:
                    key = f"{pair_sym}:risky_pump"
                    if _cooldown_ok(key, minutes=45):
                        scope = "2h" if ch2 is not None else "24h"
                        send_telegram(
                            f"🚀 *RISKY PUMP* {scope}: {pair_sym} +{change_for_risky:.2f}%\n"
                            f"Price: ${_format_price(price)} | Qty: {_format_amount(qty)} | Value: ${_format_amount(value)}\n"
                            f"{link}"
                        )

        except Exception as e:
            log.exception("alerts_monitor_loop error: %s", e)

        for _ in range(interval):
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
    log.info("Alerts interval: %dm | Wallet 24h dump/pump: %s/%s | Risky defaults dump/pump: %s/%s",
             ALERTS_INTERVAL_MINUTES, DUMP_ALERT_24H_PCT, PUMP_ALERT_24H_PCT, RISKY_DUMP_DEFAULT, RISKY_PUMP_DEFAULT)

    # Threads (wrapped)
    threads = []
    threads.append(run_with_restart(wallet_monitor_loop, "wallet_monitor"))
    threads.append(run_with_restart(monitor_tracked_pairs_loop, "pairs_monitor"))
    threads.append(run_with_restart(discovery_loop, "discovery"))
    threads.append(run_with_restart(intraday_report_loop, "intraday_report"))
    threads.append(run_with_restart(end_of_day_scheduler_loop, "eod_scheduler"))
    threads.append(run_with_restart(alerts_monitor_loop, "alerts_monitor"))

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
