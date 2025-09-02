#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Complete Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner
Auto-discovery, PnL (realized & unrealized), intraday/EOD reports, ATH tracking, swap reconciliation,
Alerts Monitor (dump/pump) και Short-term Guard μετά από buy, με mini-summary ανά trade.
Drop-in για Railway worker. Χρησιμοποιεί ENV variables.
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
CRONOSCAN_API      = os.getenv("CRONOSCAN_API")  # optional snapshot

# Optional seeding (still supported)
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")
TOKENS             = os.getenv("TOKENS", "")

# Poll/monitor settings
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL          = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL             = int(os.getenv("DEX_POLL", "60"))
PRICE_WINDOW         = int(os.getenv("PRICE_WINDOW", "3"))
SPIKE_THRESHOLD      = float(os.getenv("SPIKE_THRESHOLD", "8.0"))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

# Discovery
DISCOVER_ENABLED   = os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on")
DISCOVER_QUERY     = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT     = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL      = int(os.getenv("DISCOVER_POLL", "120"))

# Time / reports
TZ                 = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS     = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR           = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE         = int(os.getenv("EOD_MINUTE", "59"))

# Alerts Monitor (new)
ALERTS_INTERVAL_MIN       = int(os.getenv("ALERTS_INTERVAL_MIN", "15"))
DUMP_ALERT_24H_PCT        = float(os.getenv("DUMP_ALERT_24H_PCT", "15.0"))  # -15%
PUMP_ALERT_24H_PCT        = float(os.getenv("PUMP_ALERT_24H_PCT", "20.0"))  # +20%
RISKY_INTERVAL_MIN        = int(os.getenv("RISKY_INTERVAL_MIN", "15"))
RISKY_DUMP_24H_PCT        = float(os.getenv("RISKY_DUMP_24H_PCT", "15.0"))
RISKY_PUMP_24H_PCT        = float(os.getenv("RISKY_PUMP_24H_PCT", "20.0"))

# Risky lists and custom thresholds
RISKY_SYMBOLS = [s.strip().upper() for s in os.getenv("RISKY_SYMBOLS", "").split(",") if s.strip()]
# Example: "MERY/WCRO=-10;MOON/WCRO=-20"
RISKY_THRESHOLDS_RAW = os.getenv("RISKY_THRESHOLDS", "")
_risky_pair_custom = {}
if RISKY_THRESHOLDS_RAW:
    for item in RISKY_THRESHOLDS_RAW.split(";"):
        item = item.strip()
        if not item or "=" not in item: continue
        k, v = item.split("=", 1)
        try:
            _risky_pair_custom[k.strip().upper()] = float(v.strip())
        except Exception:
            pass

# Risky pump override (optional)
RISKY_PUMP_THRESHOLDS_RAW = os.getenv("RISKY_PUMP_THRESHOLDS", "")  # "MERY/WCRO=25;MOON/WCRO=30"
_risky_pair_pump_custom = {}
if RISKY_PUMP_THRESHOLDS_RAW:
    for item in RISKY_PUMP_THRESHOLDS_RAW.split(";"):
        item = item.strip()
        if not item or "=" not in item: continue
        k, v = item.split("=", 1)
        try:
            _risky_pair_pump_custom[k.strip().upper()] = float(v.strip())
        except Exception:
            pass

# --- Interactive / Guard / ATH (new) ---
MINI_SUMMARY_AFTER_TRADE = os.getenv("MINI_SUMMARY_AFTER_TRADE", "true").lower() in ("1","true","yes","on")
GUARD_LOOKBACK_MIN       = int(os.getenv("GUARD_LOOKBACK_MIN", "30"))     # πόσα λεπτά μετά από BUY
GUARD_DROP_PCT           = float(os.getenv("GUARD_DROP_PCT", "12.0"))     # alert αν -X% από buy
ATH_ALERT_COOLDOWN_MIN   = int(os.getenv("ATH_ALERT_COOLDOWN_MIN", "60")) # cooldown για spam control

# ----------------------- Constants -----------------------
ETHERSCAN_V2_URL   = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID     = 25
DEX_BASE_PAIRS     = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS    = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH    = "https://api.dexscreener.com/latest/dex/search"
CRONOS_TX          = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL       = "https://api.telegram.org/bot{token}/sendMessage"
DATA_DIR           = "/app/data"

# ----------------------- Logging -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | INFO | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wallet-monitor")

# ----------------------- HTTP session -----------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})

# simple rate-limiting (best-effort)
_last_req_ts = 0.0
def _rl_sleep(min_interval=0.15):
    global _last_req_ts
    now = time.time()
    diff = now - _last_req_ts
    if diff < min_interval:
        time.sleep(min_interval - diff)
    _last_req_ts = time.time()

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
_alert_last_sent = {}  # key -> ts

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
    if r is None: return None
    if not getattr(r, "ok", False): return None
    try:
        return r.json()
    except Exception:
        return None

def send_telegram(message: str) -> bool:
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            return False
        url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        _rl_sleep(0.05)
        r = SESSION.post(url, data=payload, timeout=12)
        return r.status_code == 200
    except Exception:
        return False

# ----------------------- ATH persistence -----------------------
ATH_FILE = os.path.join(DATA_DIR, "ath.json")
_ath_by_token = defaultdict(float)
_ath_last_alert_ts = defaultdict(float)

def _ath_key(symbol: str, token_addr: str | None) -> str:
    if token_addr and isinstance(token_addr, str) and token_addr.startswith("0x"):
        return token_addr.lower()
    return (symbol or "").upper()

def load_ath():
    try:
        obj = read_json(ATH_FILE, default={})
        if isinstance(obj, dict):
            for k, v in obj.items():
                try:
                    _ath_by_token[k] = float(v)
                except Exception:
                    continue
    except Exception:
        pass

def save_ath():
    try:
        write_json(ATH_FILE, dict(_ath_by_token))
    except Exception:
        pass

def update_ath_and_maybe_alert(symbol: str, token_addr: str | None, price: float):
    try:
        if not price or price <= 0:
            return
        k = _ath_key(symbol, token_addr)
        prev = float(_ath_by_token.get(k, 0.0))
        if price > prev + 1e-12:
            _ath_by_token[k] = price
            save_ath()
            last_ts = _ath_last_alert_ts.get(k, 0.0)
            if time.time() - last_ts >= ATH_ALERT_COOLDOWN_MIN * 60:
                _ath_last_alert_ts[k] = time.time()
                sym = symbol or (token_addr[:8] if token_addr else "?")
                send_telegram(f"🏆 *New ATH* on {sym}: ${_format_price(price)} (prev ${_format_price(prev)})")
    except Exception:
        pass

# ----------------------- Price helpers -----------------------
def _top_price_from_pairs_pricehelpers(pairs):
    if not pairs: return None
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            chain_id = str(p.get("chainId","")).lower()
            if chain_id and chain_id != "cronos": continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0: continue
            if liq > best_liq:
                best_liq, best = liq, price
        except Exception:
            continue
    return best

def _price_from_dexscreener_token(token_addr):
    try:
        _rl_sleep()
        url = f"{DEX_BASE_TOKENS}/cronos/{token_addr}"
        r = SESSION.get(url, timeout=10)
        data = safe_json(r)
        if data:
            return _top_price_from_pairs_pricehelpers(data.get("pairs"))
    except Exception:
        pass
    return None

def _price_from_dexscreener_search(symbol_or_query):
    try:
        _rl_sleep()
        r = SESSION.get(DEX_BASE_SEARCH, params={"q": symbol_or_query}, timeout=12)
        data = safe_json(r)
        if data:
            return _top_price_from_pairs_pricehelpers(data.get("pairs"))
    except Exception:
        pass
    return None

def _price_from_coingecko_contract(token_addr):
    try:
        addr = token_addr.lower()
        url = "https://api.coingecko.com/api/v3/simple/token_price/cronos"
        params = {"contract_addresses": addr, "vs_currencies": "usd"}
        _rl_sleep()
        r = SESSION.get(url, params=params, timeout=12)
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
        _rl_sleep()
        r = SESSION.get(url, params={"ids": ids, "vs_currencies": "usd"}, timeout=8)
        data = safe_json(r)
        if not data: return None
        for idk in ("cronos", "crypto-com-chain"):
            if idk in data and "usd" in data[idk]:
                return float(data[idk]["usd"])
    except Exception:
        pass
    return None

def get_price_usd(symbol_or_addr: str):
    if not symbol_or_addr: return None
    key = symbol_or_addr.strip().lower()
    now_ts = time.time()
    cached = PRICE_CACHE.get(key)
    if cached:
        price, ts = cached
        if now_ts - ts < PRICE_CACHE_TTL:
            return price
    price = None
    if key in ("cro", "wcro", "w-cro", "wrappedcro", "wrapped cro"):
        price = _price_from_dexscreener_search("cro usdt") or _price_from_dexscreener_search("wcro usdt") or _price_from_coingecko_ids_for_cro()
    elif key.startswith("0x") and len(key) == 42:
        price = _price_from_dexscreener_token(key) or _price_from_coingecko_contract(key) or _price_from_dexscreener_search(key)
    else:
        price = _price_from_dexscreener_search(key) or (len(key) <= 8 and _price_from_dexscreener_search(f"{key} usdt")) or None
    PRICE_CACHE[key] = (price, now_ts)
    return price

def get_token_price(chain: str, token_address: str):
    try:
        # 1) /tokens/{chain}/{addr}
        _rl_sleep()
        url = f"https://api.dexscreener.com/latest/dex/tokens/{chain}/{token_address}"
        r = SESSION.get(url, timeout=10)
        if r.status_code == 200:
            d = safe_json(r)
            if d and "pairs" in d and d["pairs"]:
                p = _top_price_from_pairs_pricehelpers(d["pairs"])
                if p: return float(p)
        # 2) /tokens/{addr}
        _rl_sleep()
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = SESSION.get(url, timeout=10)
        if r.status_code == 200:
            d = safe_json(r)
            if d and "pairs" in d and d["pairs"]:
                p = _top_price_from_pairs_pricehelpers(d["pairs"])
                if p: return float(p)
        # 3) search
        p = _price_from_dexscreener_search(token_address)
        if p: return float(p)
        # 4) coingecko fallback
        cg = _price_from_coingecko_contract(token_address)
        if cg: return float(cg)
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
    try:
        _rl_sleep()
        r = SESSION.get(ETHERSCAN_V2_URL, params=params, timeout=15)
        data = safe_json(r)
        if data and str(data.get("status","")).strip() == "1" and isinstance(data.get("result"), list):
            return data["result"]
        return []
    except Exception:
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
        _rl_sleep()
        r = SESSION.get(ETHERSCAN_V2_URL, params=params, timeout=15)
        data = safe_json(r)
        if data and str(data.get("status","")).strip() == "1" and isinstance(data.get("result"), list):
            return data["result"]
        return []
    except Exception:
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
        _position_qty[token_key] = qty + buy_qty
        _position_cost[token_key] = cost + buy_qty * (price_usd or 0.0)
    elif signed_amount < -EPSILON:
        sell_qty_req = -signed_amount
        if qty > EPSILON:
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
    if not h or h in _seen_tx_hashes: return
    _seen_tx_hashes.add(h)

    val_raw = tx.get("value", "0")
    try: amount_cro = int(val_raw) / 10**18
    except Exception:
        try: amount_cro = float(val_raw)
        except Exception: amount_cro = 0.0

    frm  = (tx.get("from") or "").lower()
    to   = (tx.get("to") or "").lower()
    ts   = int(tx.get("timeStamp") or 0)
    dt   = datetime.fromtimestamp(ts) if ts > 0 else now_dt()

    sign = +1 if to == WALLET_ADDRESS else (-1 if frm == WALLET_ADDRESS else 0)
    if sign == 0 or abs(amount_cro) <= EPSILON: return

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
    if not h: return
    frm  = (t.get("from") or "").lower()
    to   = (t.get("to") or "").lower()
    if WALLET_ADDRESS not in (frm, to): return

    token_addr = (t.get("contractAddress") or "").lower()
    symbol     = t.get("tokenSymbol") or token_addr[:8]
    try: decimals = int(t.get("tokenDecimal") or 18)
    except Exception: decimals = 18

    val_raw = t.get("value", "0")
    try: amount = int(val_raw) / (10 ** decimals)
    except Exception:
        try: amount = float(val_raw)
        except Exception: amount = 0.0

    ts = int(t.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(ts) if ts > 0 else now_dt()

    sign = +1 if to == WALLET_ADDRESS else -1
    price = get_price_usd(token_addr) or 0.0
    usd_value = sign * amount * price

    _token_balances[token_addr] += sign * amount
    _token_meta[token_addr] = {"symbol": symbol, "decimals": decimals}

    # --- Auto-adopt pair for buys ---
    if sign > 0 and token_addr and token_addr.startswith("0x"):
        try:
            pairs = fetch_token_pairs("cronos", token_addr)
            if pairs:
                top = pairs[0]
                pair_addr = top.get("pairAddress")
                if pair_addr:
                    ensure_tracking_pair("cronos", pair_addr, meta=top)
        except Exception:
            pass

    # --- Guard track BUYs ---
    if sign > 0 and price and price > 0 and token_addr and token_addr.startswith("0x"):
        _recent_buys[token_addr].append({"ts": time.time(), "price": float(price), "qty": float(abs(amount)), "hash": h})
        if len(_recent_buys[token_addr]) > 20:
            _recent_buys[token_addr] = _recent_buys[token_addr][-20:]

    # --- ATH update ---
    try:
        update_ath_and_maybe_alert(symbol, token_addr, float(price or 0.0))
    except Exception:
        pass

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

    # --- Mini inline summary ---
    if MINI_SUMMARY_AFTER_TRADE:
        try:
            line = inline_asset_summary_line(symbol, token_addr)
            send_telegram(f"ℹ️ *Today summary*\n{line}")
        except Exception:
            pass

# ----------------------- Wallet monitor loop -----------------------
def wallet_monitor_loop():
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
        try:
            txs = fetch_latest_wallet_txs(limit=25)
            if txs:
                for tx in reversed(txs):
                    if not isinstance(tx, dict): continue
                    handle_native_tx(tx)
        except Exception:
            pass
        try:
            toks = fetch_latest_token_txs(limit=50)
            if toks:
                for t in reversed(toks):
                    h = t.get("hash")
                    if h and h in last_tokentx_seen: continue
                    handle_erc20_tx(t)
                    if h: last_tokentx_seen.add(h)
                if len(last_tokentx_seen) > 500:
                    last_tokentx_seen = set(list(last_tokentx_seen)[-300:])
        except Exception:
            pass

        for _ in range(WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Dexscreener pair monitor + discovery -----------------------
def slug(chain: str, pair_address: str) -> str:
    return f"{chain}/{pair_address}".lower()

def fetch_pair(slug_str: str):
    url = f"{DEX_BASE_PAIRS}/{slug_str}"
    try:
        _rl_sleep()
        r = SESSION.get(url, timeout=12)
        return safe_json(r)
    except Exception:
        return None

def fetch_token_pairs(chain: str, token_address: str):
    url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
    try:
        _rl_sleep()
        r = SESSION.get(url, timeout=12)
        data = safe_json(r)
        if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
            return data["pairs"]
        return []
    except Exception:
        return []

def fetch_search(query: str):
    try:
        _rl_sleep()
        r = SESSION.get(DEX_BASE_SEARCH, params={"q": query}, timeout=15)
        data = safe_json(r)
        if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
            return data["pairs"]
        return []
    except Exception:
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
        ds_link = f"https://dexscreener.com/{chain}/{pair_address}"
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
    if not hist or len(hist) < 2: return None
    first, last = hist[0], hist[-1]
    if not first: return None
    pct = (last - first) / first * 100.0
    return pct if abs(pct) >= SPIKE_THRESHOLD else None

def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        log.info("No tracked pairs; monitor waits until discovery/seed adds some.")
    else:
        send_telegram(f"🚀 Dexscreener monitor started for: {', '.join(sorted(_tracked_pairs))}")

    while not shutdown_event.is_set():
        if not _tracked_pairs:
            time.sleep(DEX_POLL); continue
        for s in list(_tracked_pairs):
            try:
                data = fetch_pair(s)
                if not data: continue
                pair = None
                if isinstance(data, dict) and isinstance(data.get("pair"), dict):
                    pair = data["pair"]
                elif isinstance(data, dict) and isinstance(data.get("pairs"), list) and data["pairs"]:
                    pair = data["pairs"][0]
                else:
                    continue

                price_val = None
                try: price_val = float(pair.get("priceUsd") or 0)
                except Exception: price_val = None

                vol_h1 = None
                vol = pair.get("volume") or {}
                if isinstance(vol, dict):
                    try: vol_h1 = float(vol.get("h1") or 0)
                    except Exception: vol_h1 = None

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

                last_tx = pair.get("lastTx") or {}
                last_tx_hash = last_tx.get("hash") if isinstance(last_tx, dict) else None
                if last_tx_hash:
                    prev_tx = _last_pair_tx.get(s)
                    if prev_tx != last_tx_hash:
                        _last_pair_tx[s] = last_tx_hash
                        send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx_hash)}")
            except Exception:
                pass

        for _ in range(DEX_POLL):
            if shutdown_event.is_set(): break
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
        if not t.startswith("cronos/"): continue
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
                if str(p.get("chainId", "")).lower() != "cronos": continue
                pair_addr = p.get("pairAddress")
                if not pair_addr: continue
                s = slug("cronos", pair_addr)
                if s in _tracked_pairs: continue
                ensure_tracking_pair("cronos", pair_addr, meta=p)
                adopted += 1
                if adopted >= DISCOVER_LIMIT: break
        except Exception:
            pass
        for _ in range(DISCOVER_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Wallet snapshot (Cronoscan optional) -----------------------
def get_wallet_balances_snapshot(address):
    balances = {}
    addr = (address or WALLET_ADDRESS or "").lower()
    if CRONOSCAN_API:
        try:
            url = "https://api.cronoscan.com/api"
            params = {"module":"account","action":"tokenlist","address":addr,"apikey":CRONOSCAN_API}
            _rl_sleep()
            r = SESSION.get(url, params=params, timeout=10)
            data = safe_json(r)
            if isinstance(data, dict) and isinstance(data.get("result"), list):
                for tok in data.get("result", []):
                    sym = tok.get("symbol") or tok.get("tokenSymbol") or tok.get("name") or ""
                    try: dec = int(tok.get("decimals") or tok.get("tokenDecimal") or 18)
                    except Exception: dec = 18
                    bal_raw = tok.get("balance") or tok.get("tokenBalance") or "0"
                    try: amt = int(bal_raw) / (10 ** dec)
                    except Exception:
                        try: amt = float(bal_raw)
                        except Exception: amt = 0.0
                    if sym:
                        balances[sym] = balances.get(sym, 0.0) + amt
        except Exception:
            pass

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
        try: update_ath_and_maybe_alert("CRO", None, cro_price)
        except Exception: pass
        rem_qty = _position_qty.get("CRO",0.0)
        rem_cost= _position_cost.get("CRO",0.0)
        if rem_qty > EPSILON:
            unrealized += (cro_val - rem_cost)

    # Tokens
    for addr, amt in list(_token_balances.items()):
        if addr == "CRO": continue
        amt = max(0.0, amt)
        if amt <= EPSILON: continue
        meta = _token_meta.get(addr,{})
        sym = meta.get("symbol") or addr[:8]
        if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
            price = get_token_price("cronos", addr) or get_price_usd(addr) or 0.0
        else:
            price = get_price_usd(sym) or get_price_usd(addr) or 0.0
        val = amt * price
        total += val
        breakdown.append({"token":sym,"token_addr":addr,"amount":amt,"price_usd":price,"usd_value":val})
        try: update_ath_and_maybe_alert(sym, addr, price)
        except Exception: pass
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

# ----------------------- Per-asset summarize (today) -----------------------
def inline_asset_summary_line(symbol: str, token_addr: str | None) -> str:
    path = data_file_for_today()
    data = read_json(path, default={"entries": []})
    entries = data.get("entries", [])

    flow = 0.0
    realized = 0.0
    qty = 0.0
    last_price = None

    for e in entries:
        if symbol and e.get("token") != symbol:
            continue
        if token_addr:
            if (e.get("token_addr") or "").lower() != (token_addr or "").lower():
                continue
        flow += float(e.get("usd_value") or 0.0)
        realized += float(e.get("realized_pnl") or 0.0)
        qty += float(e.get("amount") or 0.0)
        p = e.get("price_usd")
        if _nonzero(p):
            last_price = float(p)

    live_price = last_price
    if live_price is None:
        if token_addr and token_addr.startswith("0x"):
            live_price = get_token_price("cronos", token_addr) or 0.0
        else:
            live_price = get_price_usd(symbol) or 0.0

    return (
        f"• {symbol}: flow ${_format_amount(flow)} | realized ${_format_amount(realized)} | "
        f"today qty {_format_amount(qty)} | price ${_format_price(live_price)}"
    )
# ----------------------- Per-asset summarize (today, clean) -----------------------
def summarize_today_per_asset():
    """
    Επιστρέφει dict ανά asset με:
      - symbol, token_addr
      - qty_in_today, qty_out_today, net_qty_today
      - usd_in_today, usd_out_today, net_flow_today
      - realized_today (άθροισμα realized από entries)
      - price_now, open_qty_now, unreal_now (μόνο αν open_qty_now > 0)
    Χρησιμοποιεί το _position_qty/_position_cost για το τρέχον open position.
    Σέβεται tokens με τιμή 0 (π.χ. dead tokens -> value 0).
    """
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": []})
    entries = data.get("entries", [])

    per = {}
    # Pass 1: συγκεντρωτικά ανά token-symbol (όπως γράφεται στα entries)
    for e in entries:
        tok  = (e.get("token") or "?").upper()
        addr = e.get("token_addr")
        amt  = float(e.get("amount") or 0.0)
        usd  = float(e.get("usd_value") or 0.0)
        rp   = float(e.get("realized_pnl") or 0.0)
        pu   = float(e.get("price_usd") or 0.0)

        row = per.setdefault(tok, {
            "symbol": tok, "token_addr": addr,
            "qty_in_today": 0.0, "qty_out_today": 0.0, "net_qty_today": 0.0,
            "usd_in_today": 0.0, "usd_out_today": 0.0, "net_flow_today": 0.0,
            "realized_today": 0.0,
            "price_now": None,
            "open_qty_now": 0.0, "unreal_now": 0.0,
            "last_seen_price": None
        })

        if amt > 0:
            row["qty_in_today"]  += amt
            row["usd_in_today"]  += usd
        elif amt < 0:
            row["qty_out_today"] += (-amt)
            row["usd_out_today"] += (-usd)

        row["net_qty_today"]  += amt
        row["net_flow_today"] += usd
        row["realized_today"] += rp

        # Κράτα τελευταία τιμή που είδαμε στην ημέρα (αν > 0)
        if pu > 0:
            row["last_seen_price"] = pu

        # αν δεν έχουμε ήδη token_addr, κράτα το πρώτο non-empty
        if not row["token_addr"] and addr:
            row["token_addr"] = addr

    # Pass 2: ζωντανά στοιχεία (τιμή τώρα, open position, unreal)
    for tok, row in per.items():
        addr = row["token_addr"]

        # Ποσότητα που κρατάμε τώρα (από global state, όχι μόνο σημερινό)
        if tok == "CRO":
            open_qty = float(_position_qty.get("CRO", 0.0))
            rem_cost = float(_position_cost.get("CRO", 0.0))
        else:
            # Το κλειδί για τα globals είναι το addr για ERC20, αλλιώς το symbol
            key = addr if (isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42) else tok
            open_qty = float(_position_qty.get(key, 0.0))
            rem_cost = float(_position_cost.get(key, 0.0))

        row["open_qty_now"] = max(0.0, open_qty)

        # Live τιμή: προτίμησε contract-based αν έχουμε addr
        price_now = None
        if tok == "CRO":
            price_now = get_price_usd("CRO") or 0.0
        else:
            if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
                price_now = get_token_price("cronos", addr)
                if not price_now:
                    # fallback: δοκίμασε και generic
                    price_now = get_price_usd(addr) or 0.0
            else:
                price_now = get_price_usd(tok) or 0.0

        # Αν δεν βρήκαμε live τιμή και έχουμε last_seen_price, κράτα την· αλλιώς 0 (dead tokens ok)
        if (price_now is None or price_now <= 0) and row.get("last_seen_price"):
            price_now = float(row["last_seen_price"])

        row["price_now"] = float(price_now or 0.0)

        # Unrealized μόνο για open_qty_now > 0 και price_now > 0
        if row["open_qty_now"] > 0 and row["price_now"] > 0:
            mtm_now = row["open_qty_now"] * row["price_now"]
            row["unreal_now"] = mtm_now - rem_cost
        else:
            row["unreal_now"] = 0.0

        # Αν το net_qty_today βγει αρνητικό (καθαρές πωλήσεις), δεν το εμφανίζουμε σαν "αρνητικό holding"
        # Το realized_today το έχουμε ήδη αθροίσει από τα entries.
        if row["net_qty_today"] < 0:
            row["net_qty_today"] = 0.0

    return per

# ----------------------- Report builder (daily/intraday) -----------------------
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
        if _nonzero(e.get("price_usd", 0.0)):
            per_asset_last_price[tok] = float(e.get("price_usd"))
        if addr and tok not in per_asset_token_addr:
            per_asset_token_addr[tok] = addr

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

    # --- Per-Asset Summary (Today) with totals ---
    per = summarize_today_per_asset()
    if per:
        lines.append("\n*Per-Asset Summary (Today):*")
        # ταξινόμηση με βάση το απόλυτο net_flow_today (μεγαλύτερα πρώτα)
        order = sorted(per.values(), key=lambda r: abs(r["net_flow_today"]), reverse=True)
        LIMIT = 15
        for row in order[:LIMIT]:
            tok   = row["symbol"]
            flow  = row["net_flow_today"]
            rsum  = row["realized_today"]
            qty_t = row["net_qty_today"]      # καθαρή ποσότητα που προστέθηκε σήμερα (δεν δείχνουμε αρνητικά)
            px    = row["price_now"]
            oq    = row["open_qty_now"]
            unrl  = row["unreal_now"]

            # Γραμμή σύνοψης (σήμερα)
            base = f"  • {tok}: flow ${_format_amount(flow)} | realized ${_format_amount(rsum)}"

            # Αν είχαμε καθαρή προσθήκη σήμερα, δείξ’ την ποσότητα και την τιμή
            if qty_t > 0:
                base += f" | today qty {_format_amount(qty_t)} @ ${_format_price(px)}"

            # Αν υπάρχει ανοιχτή θέση τώρα, δείξε και το unrealized
            if oq > 0:
                base += f" | open {_format_amount(oq)} @ ${_format_price(px)} | unreal ${_format_amount(unrl)}"

            lines.append(base)

        if len(order) > LIMIT:
            lines.append(f"  …and {len(order)-LIMIT} more.")

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
        except Exception:
            pass
        for _ in range(30):
            if shutdown_event.is_set(): break
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
        if shutdown_event.is_set(): break
        try:
            txt = build_day_report_text()
            send_telegram("🟢 *End of Day Report*\n" + txt)
        except Exception:
            pass

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

# ----------------------- Alerts Monitor (dump/pump 24h & risky) -----------------------
def _cooldown_key(kind, ident):
    return f"{kind}:{ident}"

def _cooldown_ok(kind, ident, minutes=30):
    key = _cooldown_key(kind, ident)
    last = _alert_last_sent.get(key, 0.0)
    if time.time() - last >= minutes*60:
        _alert_last_sent[key] = time.time()
        return True
    return False

def alerts_monitor_loop():
    send_telegram(
        f"📡 Alerts monitor enabled. Interval: {ALERTS_INTERVAL_MIN}m | "
        f"Wallet 24h dump/pump: -{DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT} | "
        f"Risky defaults: -{RISKY_DUMP_24H_PCT}/{RISKY_PUMP_24H_PCT}"
    )
    while not shutdown_event.is_set():
        try:
            # Build watchlists
            # all wallet coins: from balances (symbols & addrs seen)
            wallet_addrs = [k for k in _token_balances.keys() if k != "CRO" and isinstance(k, str) and k.startswith("0x")]
            # risky = env symbols (we'll match by base symbol)
            risky_syms = set(RISKY_SYMBOLS or [])

            # 1) All wallet coins -> 24h monitoring via token top pair % change
            for addr in wallet_addrs:
                try:
                    pairs = fetch_token_pairs("cronos", addr)
                    if not pairs: continue
                    p = pairs[0]
                    pair_addr = p.get("pairAddress")
                    base_sym  = (p.get("baseToken") or {}).get("symbol") or addr[:8]
                    ds_link   = f"https://dexscreener.com/cronos/{pair_addr}" if pair_addr else ""
                    # 24h change
                    change24 = None
                    try:
                        change24 = float((p.get("priceChange") or {}).get("h24") or 0.0)
                    except Exception:
                        change24 = None
                    if change24 is None: continue
                    # dump
                    if change24 <= -abs(DUMP_ALERT_24H_PCT):
                        ident = f"WALLET24:DUMP:{addr}"
                        if _cooldown_ok("WALLET24DUMP", addr, minutes=30):
                            send_telegram(
                                f"🔻 *24h Dump* {base_sym} {change24:.2f}%\n{ds_link}"
                            )
                    # pump
                    if change24 >= abs(PUMP_ALERT_24H_PCT):
                        ident = f"WALLET24:PUMP:{addr}"
                        if _cooldown_ok("WALLET24PUMP", addr, minutes=30):
                            send_telegram(
                                f"🟢 *24h Pump* {base_sym} +{change24:.2f}%\n{ds_link}"
                            )
                except Exception:
                    continue

            # 2) Risky tokens -> try 2h if available, else 24h. Custom thresholds by "SYM/QUOTE"
            # We'll discover pairs from fetch_search with query = symbol (fallback).
            for sym in list(risky_syms):
                try:
                    pairs = fetch_search(sym)
                    if not pairs: continue
                    # pick first Cronos
                    rp = None
                    for p in pairs:
                        if str(p.get("chainId","")).lower() == "cronos":
                            rp = p; break
                    if not rp: rp = pairs[0]
                    pair_addr = rp.get("pairAddress")
                    base_sym  = (rp.get("baseToken") or {}).get("symbol") or sym
                    quote_sym = (rp.get("quoteToken") or {}).get("symbol") or ""
                    pair_key  = f"{base_sym.upper()}/{quote_sym.upper()}" if quote_sym else base_sym.upper()
                    ds_link   = f"https://dexscreener.com/cronos/{pair_addr}" if pair_addr else ""
                    # prefer 2h if present, else 24h
                    change = None
                    try:
                        chg = rp.get("priceChange") or {}
                        change = float(chg.get("h2")) if _nonzero(chg.get("h2")) else float(chg.get("h24") or 0.0)
                    except Exception:
                        change = None
                    if change is None: continue
                    # thresholds
                    dump_th = _risky_pair_custom.get(pair_key, RISKY_DUMP_24H_PCT)
                    pump_th = _risky_pair_pump_custom.get(pair_key, RISKY_PUMP_24H_PCT)
                    # dump
                    if change <= -abs(dump_th):
                        if _cooldown_ok("RISKYDUMP", pair_key, minutes=20):
                            send_telegram(f"⚠️ *Risky dump* {pair_key} {change:.2f}% (thr {dump_th}%)\n{ds_link}")
                    # pump
                    if change >= abs(pump_th):
                        if _cooldown_ok("RISKYPUMP", pair_key, minutes=20):
                            send_telegram(f"🚀 *Risky pump* {pair_key} +{change:.2f}% (thr {pump_th}%)\n{ds_link}")
                except Exception:
                    continue

        except Exception:
            pass

        # sleep
        for _ in range(ALERTS_INTERVAL_MIN*60):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Guard monitor (short-term drop after buy) -----------------------
_recent_buys = defaultdict(list)   # token_addr -> [{ts, price, qty, hash}]
_guard_alerted = set()

def guard_monitor_loop():
    send_telegram(f"🛡 Guard monitor enabled (lookback {GUARD_LOOKBACK_MIN}m, drop {GUARD_DROP_PCT}%).")
    while not shutdown_event.is_set():
        try:
            now_ts = time.time()
            cutoff = now_ts - GUARD_LOOKBACK_MIN * 60
            for addr, buys in list(_recent_buys.items()):
                # current price
                try:
                    cur_price = 0.0
                    if addr and addr.startswith("0x"):
                        cur_price = get_token_price("cronos", addr) or 0.0
                    if not cur_price or cur_price <= 0:
                        continue
                except Exception:
                    continue
                kept = []
                for b in buys:
                    bts = float(b.get("ts") or 0.0)
                    if bts < cutoff:
                        continue
                    kept.append(b)
                    key = f"{addr}:{int(bts)}:{b.get('hash')}"
                    if key in _guard_alerted:
                        continue
                    buy_price = float(b.get("price") or 0.0)
                    if buy_price <= 0:
                        continue
                    change_pct = (cur_price - buy_price) / buy_price * 100.0
                    if change_pct <= -abs(GUARD_DROP_PCT):
                        meta = _token_meta.get(addr, {})
                        sym = meta.get("symbol") or addr[:8]
                        link = ""
                        try:
                            pairs = fetch_token_pairs("cronos", addr)
                            if pairs:
                                paddr = pairs[0].get("pairAddress")
                                if paddr:
                                    link = f"\nhttps://dexscreener.com/cronos/{paddr}"
                        except Exception:
                            pass
                        send_telegram(
                            "⚠️ *Guard alert*\n"
                            f"{sym}: {change_pct:.2f}% από buy σε {GUARD_LOOKBACK_MIN}m\n"
                            f"Buy @ ${_format_price(buy_price)} → Now ${_format_price(cur_price)}" + (link or "")
                        )
                        _guard_alerted.add(key)
                _recent_buys[addr] = kept
        except Exception:
            pass
        for _ in range(15):
            if shutdown_event.is_set(): break
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
                    if shutdown_event.is_set(): break
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
    log.info("Alerts interval: %sm | Wallet 24h dump/pump: -%s/%s | Risky defaults dump/pump: -%s/%s",
             ALERTS_INTERVAL_MIN, DUMP_ALERT_24H_PCT, PUMP_ALERT_24H_PCT, RISKY_DUMP_24H_PCT, RISKY_PUMP_24H_PCT)

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
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt: shutting down...")
        shutdown_event.set()

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
