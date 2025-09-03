#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Wallet monitor (Cronos via Etherscan v2) + Dexscreener scanner
Auto-discovery (with filters), PnL (realized & unrealized), intraday/EOD reports,
ATH tracking (persisted), swap reconciliation, Alerts (24h + optional 2h),
Guard after BUY (entry pump/dump + trailing), bootstrap balances από ιστορικό,
HTTP retry/backoff + rate limiting, clean Telegram output.

Drop-in για Railway worker (Procfile: `worker: python3 main.py`).
Χρησιμοποιεί ENV variables (χωρίς hardcoded secrets).
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

import requests
from dotenv import load_dotenv

load_dotenv()

# ----------------------- Config / ENV -----------------------
def _as_bool(s, default=False):
    if s is None or s == "":
        return default
    return str(s).strip().lower() in ("1","true","yes","on")

def _as_float(s, default):
    try:
        return float(str(s).strip())
    except Exception:
        return float(default)

def _as_int(s, default):
    try:
        return int(str(s).strip())
    except Exception:
        return int(default)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS","") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API","")
CRONOSCAN_API      = os.getenv("CRONOSCAN_API","")  # optional

# Optional seeding
DEX_PAIRS          = os.getenv("DEX_PAIRS","")
TOKENS             = os.getenv("TOKENS","")  # "cronos/0xToken1,..."

# Poll/monitor settings
WALLET_POLL        = _as_int(os.getenv("WALLET_POLL","15"), 15)
DEX_POLL           = _as_int(os.getenv("DEX_POLL","60"), 60)
PRICE_WINDOW       = _as_int(os.getenv("PRICE_WINDOW","3"), 3)
SPIKE_THRESHOLD    = _as_float(os.getenv("SPIKE_THRESHOLD","8"), 8.0)
PRICE_MOVE_THRESHOLD = _as_float(os.getenv("PRICE_MOVE_THRESHOLD","5"), 5.0)
MIN_VOLUME_FOR_ALERT = _as_float(os.getenv("MIN_VOLUME_FOR_ALERT","0"), 0.0)

# Discovery
DISCOVER_ENABLED   = _as_bool(os.getenv("DISCOVER_ENABLED","true"), True)
DISCOVER_QUERY     = os.getenv("DISCOVER_QUERY","cronos")
DISCOVER_LIMIT     = _as_int(os.getenv("DISCOVER_LIMIT","10"), 10)
DISCOVER_POLL      = _as_int(os.getenv("DISCOVER_POLL","120"), 120)

# Discovery Filters (NEW)
DISCOVER_REQUIRE_WCRO     = _as_bool(os.getenv("DISCOVER_REQUIRE_WCRO","true"), True)
DISCOVER_MIN_LIQ_USD      = _as_float(os.getenv("DISCOVER_MIN_LIQ_USD","30000"), 30000.0)
DISCOVER_MIN_VOL24_USD    = _as_float(os.getenv("DISCOVER_MIN_VOL24_USD","5000"), 5000.0)
DISCOVER_MIN_ABS_CHANGE_PCT = _as_float(os.getenv("DISCOVER_MIN_ABS_CHANGE_PCT","10"), 10.0) # για h1/h4/h6
DISCOVER_MAX_PAIR_AGE_HOURS = _as_int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS") or "24", 24)   # only new ≤24h
DISCOVER_BASE_WHITELIST   = [x.strip().upper() for x in os.getenv("DISCOVER_BASE_WHITELIST","").split(",") if x.strip()]
DISCOVER_BASE_BLACKLIST   = [x.strip().upper() for x in os.getenv("DISCOVER_BASE_BLACKLIST","").split(",") if x.strip()]

# Time / reports
TZ                = os.getenv("TZ","Europe/Athens")
INTRADAY_HOURS    = _as_float(os.getenv("INTRADAY_HOURS","3"), 3.0)
EOD_HOUR          = _as_int(os.getenv("EOD_HOUR","23"), 23)
EOD_MINUTE        = _as_int(os.getenv("EOD_MINUTE","59"), 59)

# Alerts Monitor (24h + optional 2h for “risky”) & cooldowns
ALERTS_INTERVAL_MIN   = _as_int(os.getenv("ALERTS_INTERVAL_MIN","15"), 15)
DUMP_ALERT_24H_PCT    = _as_float(os.getenv("DUMP_ALERT_24H_PCT","-15"), -15.0)
PUMP_ALERT_24H_PCT    = _as_float(os.getenv("PUMP_ALERT_24H_PCT","20"), 20.0)
DUMP_ALERT_2H_PCT     = _as_float(os.getenv("DUMP_ALERT_2H_PCT","-15"), -15.0)
PUMP_ALERT_2H_PCT     = _as_float(os.getenv("PUMP_ALERT_2H_PCT","20"), 20.0)
ALERT_COOLDOWN_MIN    = _as_int(os.getenv("ALERT_COOLDOWN_MIN","60"), 60)

# Risky lists & custom thresholds (optional)
RISKY_SYMBOLS         = [x.strip().upper() for x in os.getenv("RISKY_SYMBOLS","").split(",") if x.strip()]
# Example: "MERY/WCRO=-10;MOON/WCRO=-20"
RISKY_THRESHOLDS_RAW  = os.getenv("RISKY_THRESHOLDS","")
RISKY_THRESHOLDS = {}
if RISKY_THRESHOLDS_RAW:
    for kv in RISKY_THRESHOLDS_RAW.split(";"):
        kv = kv.strip()
        if "=" in kv:
            k, v = kv.split("=",1)
            try:
                RISKY_THRESHOLDS[k.strip().upper()] = float(v.strip())
            except Exception:
                pass

# Guard (μετά από BUY) – window & thresholds
GUARD_WINDOW_MIN       = _as_int(os.getenv("GUARD_WINDOW_MIN","60"), 60)
GUARD_PUMP_PCT         = _as_float(os.getenv("GUARD_PUMP_PCT","20"), 20.0)    # +20% από entry
GUARD_DUMP_PCT         = _as_float(os.getenv("GUARD_DUMP_PCT","-12"), -12.0)  # -12% από entry
GUARD_TRAILING_DROP_PCT= _as_float(os.getenv("GUARD_TRAILING_DROP_PCT","-8"), -8.0)  # από peak

# ----------------------- Constants -----------------------
CRONOS_CHAINID   = 25
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL     = "https://api.telegram.org/bot{token}/sendMessage"
DATA_DIR         = "/app/data"
ATH_FILE         = os.path.join(DATA_DIR, "ath.json")

# ----------------------- Logging -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wallet-monitor")

# ----------------------- HTTP session -----------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})

# simple rate limiting + retry/backoff
_http_lock = threading.Lock()
_last_http_ts = 0.0
HTTP_MIN_INTERVAL = 0.2  # 5 req/sec max

def safe_get(url, *, params=None, timeout=12, retries=3, backoff=1.0):
    global _last_http_ts
    for attempt in range(retries):
        with _http_lock:
            now = time.time()
            wait = _last_http_ts + HTTP_MIN_INTERVAL - now
            if wait > 0:
                time.sleep(wait)
            _last_http_ts = time.time()
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            # 429/5xx -> backoff & retry
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff * (2 ** attempt))
                continue
            # 404 -> μην retry unless retries left
            if r.status_code == 404 and attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
                continue
            return r
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (2 ** attempt))
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
_token_balances   = defaultdict(float)  # "CRO" or contract -> qty
_token_meta       = {}                  # contract -> {"symbol","decimals"}

_position_qty     = defaultdict(float)
_position_cost    = defaultdict(float)
_realized_pnl_today = 0.0

EPSILON           = 1e-12
_last_intraday_sent = 0.0

# alerts state (cooldowns)
_last_alert_sent  = {}  # key -> timestamp

# Guard state: addr_or_symbol -> {entry_price, entry_ts, peak_price}
_guard_state      = {}

# ATH persistence
_ath_lock = threading.Lock()
_ath_map  = {}  # token_key -> ath_price

# Ensure data dir & timezone
try:
    os.makedirs(DATA_DIR, exist_ok=True)
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
    try:
        a = float(a)
    except Exception:
        return "0"
    if abs(a) >= 1:
        return f"{a:,.4f}"
    if abs(a) >= 0.0001:
        return f"{a:.6f}"
    return f"{a:.8f}"

def _format_price(p):
    try:
        p = float(p)
    except Exception:
        return "0"
    return f"{p:,.6f}"

def _nonzero(v, eps=1e-12):
    try:
        return abs(float(v)) > eps
    except Exception:
        return False

def safe_json(r):
    if r is None:
        return None
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
        r = SESSION.post(url, data=payload, timeout=12)
        if r.status_code != 200:
            log.warning("Telegram status %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.exception("send_telegram exception: %s", e)
        return False

def _alert_gate(key: str, cooldown_min: int) -> bool:
    """True αν ΜΠΟΡΟΥΜΕ να στείλουμε νέο alert για key."""
    now = time.time()
    last = _last_alert_sent.get(key, 0)
    if now - last >= cooldown_min * 60:
        _last_alert_sent[key] = now
        return True
    return False

# ----------------------- ATH persistence -----------------------
def _load_ath():
    global _ath_map
    with _ath_lock:
        _ath_map = read_json(ATH_FILE, default={}) or {}

def _save_ath():
    with _ath_lock:
        write_json(ATH_FILE, _ath_map)

def _update_ath(token_key: str, price: float):
    """Ενημερώνει και επιστρέφει True αν έγινε νέο ATH."""
    if price is None or price <= 0:
        return False
    prev = _ath_map.get(token_key)
    if prev is None or price > float(prev):
        _ath_map[token_key] = float(price)
        _save_ath()
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
        url = f"{DEX_BASE_TOKENS}/cronos/{token_addr}"
        r = safe_get(url, timeout=10, retries=3)
        data = safe_json(r)
        if not data:
            return None
        return _top_price_from_pairs_pricehelpers(data.get("pairs"))
    except Exception:
        return None

def _price_from_dexscreener_search(symbol_or_query):
    try:
        r = safe_get(DEX_BASE_SEARCH, params={"q": symbol_or_query}, timeout=12, retries=3)
        data = safe_json(r)
        if not data:
            return None
        return _top_price_from_pairs_pricehelpers(data.get("pairs"))
    except Exception:
        return None

def _price_from_coingecko_contract(token_addr):
    try:
        addr = token_addr.lower()
        url = "https://api.coingecko.com/api/v3/simple/token_price/cronos"
        params = {"contract_addresses": addr, "vs_currencies": "usd"}
        r = safe_get(url, params=params, timeout=12, retries=2)
        data = safe_json(r)
        if not data:
            return None
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
        r = safe_get(url, params={"ids": ids, "vs_currencies": "usd"}, timeout=8, retries=2)
        data = safe_json(r)
        if not data:
            return None
        for idk in ("cronos", "crypto-com-chain"):
            if idk in data and "usd" in data[idk]:
                return float(data[idk]["usd"])
    except Exception:
        pass
    return None

def get_price_usd(symbol_or_addr: str):
    """Γρήγορο generic price (symbol ή 0xaddr)."""
    if not symbol_or_addr:
        return None
    key = symbol_or_addr.strip().lower()

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
    return price

def get_token_price(chain: str, token_address: str):
    """Πιο ανθεκτικό lookup για contract."""
    try:
        # 1) /tokens/{chain}/{addr}
        url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
        r = safe_get(url, timeout=10, retries=3)
        if r and r.status_code == 200:
            d = safe_json(r)
            if d and "pairs" in d and d["pairs"]:
                p = _top_price_from_pairs_pricehelpers(d["pairs"])
                if p:
                    return float(p)
        # 2) /tokens/{addr}
        url = f"{DEX_BASE_TOKENS}/{token_address}"
        r = safe_get(url, timeout=10, retries=3)
        if r and r.status_code == 200:
            d = safe_json(r)
            if d and "pairs" in d and d["pairs"]:
                p = _top_price_from_pairs_pricehelpers(d["pairs"])
                if p:
                    return float(p)
        # 3) search
        p = _price_from_dexscreener_search(token_address)
        if p:
            return float(p)
        # 4) coingecko
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
    try:
        r = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)
        data = safe_json(r)
        if not data:
            return []
        if str(data.get("status","")).strip() == "1" and isinstance(data.get("result"), list):
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
        r = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)
        data = safe_json(r)
        if not data:
            return []
        if str(data.get("status","")).strip() == "1" and isinstance(data.get("result"), list):
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
    """Ξαναυπολογίζει το today's realized_pnl από τα entries (στην εκκίνηση)."""
    global _position_qty, _position_cost, _realized_pnl_today
    _position_qty.clear(); _position_cost.clear(); _realized_pnl_today = 0.0
    path = data_file_for_today()
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        return
    for e in data.get("entries", []):
        key = (e.get("token_addr") or ("CRO" if e.get("token")=="CRO" else "CRO"))
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

# ----------------------- Bootstrap balances from history -----------------------
def bootstrap_watchlist_from_history(max_pages: int = 3, page_size: int = 200):
    """
    Γεμίζει _token_balances/_token_meta από πρόσφατο ιστορικό ERC20 transfers (χωρίς CRONOSCAN).
    Στόχος: alerts/watchlist αμέσως μετά το boot.
    """
    try:
        seen = set()
        agg_bal = defaultdict(float)
        meta_map = {}
        limit = page_size
        for _ in range(max_pages):
            toks = fetch_latest_token_txs(limit=limit)
            if not toks:
                break
            duplicated = 0
            for t in toks:
                h = t.get("hash")
                if not h or h in seen:
                    duplicated += 1
                    continue
                seen.add(h)
                frm = (t.get("from") or "").lower()
                to  = (t.get("to") or "").lower()
                if WALLET_ADDRESS not in (frm, to):
                    continue
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
                sign = +1.0 if to == WALLET_ADDRESS else -1.0
                qty  = sign * amount
                if abs(qty) > EPSILON and token_addr:
                    agg_bal[token_addr] += qty
                    if token_addr not in meta_map:
                        meta_map[token_addr] = {"symbol": symbol, "decimals": decimals}
            if duplicated > (0.8 * len(toks)):
                break
        for addr, qty in agg_bal.items():
            if qty > EPSILON:
                _token_balances[addr] = max(_token_balances.get(addr, 0.0), qty)
                if addr in meta_map:
                    _token_meta[addr] = meta_map[addr]
        if _token_balances:
            try:
                sample = []
                for k, v in list(_token_balances.items())[:6]:
                    sym = _token_meta.get(k, {}).get("symbol") or ("CRO" if k == "CRO" else k[:8])
                    sample.append(f"{sym}:{_format_amount(v)}")
                send_telegram("🔎 Bootstrapped balances: " + ", ".join(sample))
            except Exception:
                pass
    except Exception as e:
        log.exception("bootstrap_watchlist_from_history error: %s", e)

# ----------------------- Cost-basis / PnL -----------------------
def _update_cost_basis(token_key: str, signed_amount: float, price_usd: float):
    global _realized_pnl_today
    qty = _position_qty[token_key]
    cost = _position_cost[token_key]
    realized = 0.0
    if signed_amount > EPSILON:
        # BUY
        buy_qty = signed_amount
        add_cost = buy_qty * max(0.0, price_usd or 0.0)
        _position_qty[token_key] = qty + buy_qty
        _position_cost[token_key] = cost + add_cost
    elif signed_amount < -EPSILON:
        # SELL
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

    sign = +1 if to == WALLET_ADDRESS else (-1 if frm == WALLET_ADDRESS else 0)
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

def _guard_key(addr_or_symbol: str) -> str:
    return addr_or_symbol or "?"

def _start_guard(addr_or_symbol: str, entry_price: float):
    if entry_price and entry_price > 0:
        _guard_state[_guard_key(addr_or_symbol)] = {
            "entry_price": float(entry_price),
            "entry_ts": time.time(),
            "peak_price": float(entry_price),
        }

def _update_guard_peak(addr_or_symbol: str, live_price: float):
    key = _guard_key(addr_or_symbol)
    st = _guard_state.get(key)
    if not st:
        return
    if live_price and live_price > st.get("peak_price", 0.0):
        st["peak_price"] = float(live_price)

def _end_guard(addr_or_symbol: str):
    _guard_state.pop(_guard_key(addr_or_symbol), None)

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

    # Προτιμάμε contract-based price
    if token_addr and token_addr.startswith("0x") and len(token_addr) == 42:
        price = get_token_price("cronos", token_addr) or 0.0
        if not price:
            price = get_price_usd(token_addr) or 0.0
    else:
        price = get_price_usd(symbol) or 0.0

    usd_value = sign * amount * price

    _token_balances[token_addr] += sign * amount
    _token_meta[token_addr] = {"symbol": symbol, "decimals": decimals}

    # Cost-basis / realized
    realized = _update_cost_basis(token_addr, sign * amount, price)

    # ATH update (per token_addr)
    if price and price > 0:
        if _update_ath(token_addr, price):
            send_telegram(f"🏆 New ATH {symbol}: ${_format_price(price)}")

    # Mini summary + Guard
    if sign > 0:
        # BUY
        _start_guard(token_addr or symbol, price)
        send_telegram(
            f"• BUY {symbol} {_format_amount(amount)} @ live ${_format_price(price)}\n"
            f"   Open: {_format_amount(_position_qty[token_addr])} {symbol} | "
            f"Avg: ${_format_price((_position_cost[token_addr]/_position_qty[token_addr]) if _position_qty[token_addr]>EPSILON else price)} | "
            f"Unreal: $0.00"
        )
    else:
        # SELL
        open_qty = _position_qty.get(token_addr, 0.0)
        avg = (_position_cost[token_addr]/open_qty) if open_qty>EPSILON else price
        send_telegram(
            f"• SELL {symbol} {_format_amount(-amount)} @ live ${_format_price(price)}\n"
            f"   Open: {_format_amount(open_qty)} {symbol} | Avg: ${_format_price(avg)} | Unreal: $0.00"
        )
        # Αν μηδενίστηκε η θέση, τελείωσε το guard
        if open_qty <= EPSILON:
            _end_guard(token_addr or symbol)

    link = CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Token TX* ({'IN' if sign>0 else 'OUT'}) {symbol}\n"
        f"Hash: {link}\n"
        f"Time: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {symbol}\n"
        f"Price: ${_format_price(price)}\n"
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
    global _seen_tx_hashes
    log.info("Wallet monitor starting; loading initial recent txs...")

    # seed avoid spam
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
        # native
        try:
            txs = fetch_latest_wallet_txs(limit=25)
            if txs:
                for tx in reversed(txs):
                    if not isinstance(tx, dict):
                        continue
                    h = tx.get("hash")
                    if h and h in _seen_tx_hashes:
                        continue
                    handle_native_tx(tx)
        except Exception as e:
            log.exception("wallet native loop error: %s", e)

        # erc20
        try:
            toks = fetch_latest_token_txs(limit=100)
            if toks:
                for t in reversed(toks):
                    h = t.get("hash")
                    if h and h in last_tokentx_seen:
                        continue
                    handle_erc20_tx(t)
                    if h:
                        last_tokentx_seen.add(h)
                if len(last_tokentx_seen) > 1000:
                    last_tokentx_seen = set(list(last_tokentx_seen)[-600:])
        except Exception as e:
            log.exception("wallet erc20 loop error: %s", e)

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
        r = safe_get(url, timeout=12, retries=3)
        return safe_json(r)
    except Exception:
        return None

def fetch_token_pairs(chain: str, token_address: str):
    url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
    try:
        r = safe_get(url, timeout=12, retries=3)
        data = safe_json(r)
        if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
            return data["pairs"]
        return []
    except Exception:
        return []

def fetch_search(query: str):
    try:
        r = safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=15, retries=3)
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
    if not hist or len(hist) < 2:
        return None
    first = hist[0]
    last  = hist[-1]
    if not first:
        return None
    pct = (last - first) / first * 100.0
    return pct if abs(pct) >= SPIKE_THRESHOLD else None

def _pair_passes_filters(p: dict) -> bool:
    try:
        # chain must be cronos
        if str(p.get("chainId","")).lower() != "cronos":
            return False

        base = (p.get("baseToken") or {})
        quote= (p.get("quoteToken") or {})
        base_sym = (base.get("symbol") or "").upper()
        quote_sym= (quote.get("symbol") or "").upper()

        if DISCOVER_REQUIRE_WCRO and quote_sym != "WCRO":
            return False

        if DISCOVER_BASE_WHITELIST and base_sym not in DISCOVER_BASE_WHITELIST:
            return False
        if DISCOVER_BASE_BLACKLIST and base_sym in DISCOVER_BASE_BLACKLIST:
            return False

        liq_usd = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq_usd < DISCOVER_MIN_LIQ_USD:
            return False

        vol = p.get("volume") or {}
        vol24 = float(vol.get("h24") or 0)
        if vol24 < DISCOVER_MIN_VOL24_USD:
            return False

        # price change thresholds (abs) – try h1/h4/h6
        change = p.get("priceChange") or {}
        cands = []
        for key in ("h1","h4","h6"):
            try:
                cands.append(float(change.get(key) or 0.0))
            except Exception:
                pass
        if not any(abs(x) >= DISCOVER_MIN_ABS_CHANGE_PCT for x in cands if x != 0):
            return False

        # age (max hours)
        # Dexscreener returns ms since epoch
        created_ms = p.get("pairCreatedAt")
        if created_ms:
            try:
                created_ts = float(created_ms)/1000.0
                age_h = (time.time() - created_ts) / 3600.0
                if age_h > DISCOVER_MAX_PAIR_AGE_HOURS:
                    return False
            except Exception:
                pass

        return True
    except Exception:
        return False

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
                if not pair:
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
                            if _alert_gate(f"SPIKE:{s}", ALERT_COOLDOWN_MIN):
                                send_telegram(f"🚨 Spike on {symbol}: {spike_pct:.2f}% over recent samples\nPrice: ${price_val:.6f} Vol1h: {vol_h1}")
                                _price_history[s].clear()
                                _last_prices[s] = price_val

                prev = _last_prices.get(s)
                if prev is not None and price_val is not None and prev != 0:
                    delta = (price_val - prev) / prev * 100.0
                    if abs(delta) >= PRICE_MOVE_THRESHOLD:
                        if _alert_gate(f"MOVE:{s}", ALERT_COOLDOWN_MIN):
                            send_telegram(f"📈 Price move on {symbol}: {delta:.2f}%\nPrice: ${price_val:.6f} (prev ${prev:.6f})")
                            _last_prices[s] = price_val

                last_tx = pair.get("lastTx") or {}
                last_tx_hash = last_tx.get("hash") if isinstance(last_tx, dict) else None
                if last_tx_hash:
                    prev_tx = _last_pair_tx.get(s)
                    if prev_tx != last_tx_hash:
                        _last_pair_tx[s] = last_tx_hash
                        if _alert_gate(f"PAIRTX:{s}", ALERT_COOLDOWN_MIN):
                            send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx_hash)}")

            except Exception as e:
                log.debug("monitor_tracked_pairs_loop error for %s: %s", s, e)

        for _ in range(DEX_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def discovery_loop():
    # 1) Προαιρετικό seeding από DEX_PAIRS
    seeds = [p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]
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
            pair_addr = p.get("pairAddress")
            if pair_addr:
                ensure_tracking_pair("cronos", pair_addr, meta=p)

    if not DISCOVER_ENABLED:
        log.info("Discovery disabled.")
        return

    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos) with filters.")
    while not shutdown_event.is_set():
        adopted = 0
        try:
            found = fetch_search(DISCOVER_QUERY)
            for p in found or []:
                if not _pair_passes_filters(p):
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
def get_wallet_balances_snapshot(address):
    balances = {}
    addr = (address or WALLET_ADDRESS or "").lower()
    if CRONOSCAN_API:
        try:
            url = "https://api.cronoscan.com/api"
            params = {"module":"account","action":"tokenlist","address":addr,"apikey":CRONOSCAN_API}
            r = safe_get(url, params=params, timeout=10, retries=2)
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
        if rem_qty > EPSILON and cro_price > 0:
            unrealized += (cro_amt * cro_price - rem_cost)

    # Tokens
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
            price = get_token_price("cronos", addr) or get_price_usd(addr) or 0.0
        else:
            price = get_price_usd(sym) or get_price_usd(addr) or 0.0
        val = amt * price
        total += val
        breakdown.append({"token":sym,"token_addr":addr,"amount":amt,"price_usd":price,"usd_value":val})
        rem_qty = _position_qty.get(addr,0.0)
        rem_cost= _position_cost.get(addr,0.0)
        if rem_qty > EPSILON and price > 0:
            unrealized += (rem_qty * price - rem_cost)
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
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])

    per = {
        "flow": defaultdict(float),
        "real": defaultdict(float),
        "qty_today": defaultdict(float),
        "last_price": {},
        "addr": {},
    }

    # Pass 1: aggregate from entries
    for e in entries:
        tok = e.get("token") or "?"
        addr = e.get("token_addr")
        per["flow"][tok] += float(e.get("usd_value") or 0.0)
        per["real"][tok] += float(e.get("realized_pnl") or 0.0)
        per["qty_today"][tok] += float(e.get("amount") or 0.0)
        p = e.get("price_usd")
        if _nonzero(p):
            per["last_price"][tok] = float(p)
        if addr and tok not in per["addr"]:
            per["addr"][tok] = addr

    # Pass 2: attach live price & open qty/unreal from globals
    lines = []
    order = sorted(per["flow"].items(), key=lambda kv: abs(kv[1]), reverse=True)
    LIMIT = 12
    shown = 0

    for tok, flow in order:
        if shown >= LIMIT:
            break
        addr = per["addr"].get(tok)
        # open qty now (global)
        if addr:
            open_qty_now = max(0.0, _token_balances.get(addr, 0.0))
            rem_qty = _position_qty.get(addr, 0.0)
            rem_cost= _position_cost.get(addr, 0.0)
        else:
            open_qty_now = max(0.0, _token_balances.get(tok, 0.0))
            rem_qty = _position_qty.get(tok, 0.0)
            rem_cost= _position_cost.get(tok, 0.0)

        # live price
        live_price = None
        if addr and isinstance(addr, str) and addr.startswith("0x"):
            live_price = get_token_price("cronos", addr) or 0.0
        if live_price is None or live_price == 0:
            live_price = per["last_price"].get(tok, 0.0)

        qty_today = per["qty_today"].get(tok, 0.0)
        real_today= per["real"].get(tok, 0.0)

        line = f"  • {tok}: flow ${_format_amount(flow)} | realized ${_format_amount(real_today)} | today qty {_format_amount(qty_today)}"
        if live_price and live_price > 0:
            line += f" | price ${_format_price(live_price)}"
        # Unreal only for open positive qty
        if rem_qty > EPSILON and live_price and live_price > 0:
            unreal = rem_qty * live_price - rem_cost
            line += f" | unreal ${_format_amount(unreal)}"
        lines.append(line)
        shown += 1

    return lines

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

    # Per-asset today (clean)
    per_lines = summarize_today_per_asset()
    if per_lines:
        lines.append("\n*Per-Asset Summary (Today):*")
        lines.extend(per_lines)

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

# ----------------------- Alerts Monitor (dump/pump 24h & risky/2h) -----------------------
def _pair_link(pair_addr: str) -> str:
    return f"https://dexscreener.com/cronos/{pair_addr}"

def _find_top_pair_for_symbol(symbol: str):
    """Χρησιμοποιούμε το search για να βρούμε Cronos top pair & meta."""
    try:
        res = fetch_search(symbol)
        for p in res or []:
            if str(p.get("chainId","")).lower() != "cronos":
                continue
            return p
    except Exception:
        pass
    return None

def alerts_monitor_loop():
    send_telegram(f"🛰 Alerts monitor active every {ALERTS_INTERVAL_MIN}m. Wallet 24h dump/pump: {DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT}.")
    while not shutdown_event.is_set():
        try:
            # Build watchlist: όλα τα tokens που έχουμε θετικό balance
            watch_contracts = []
            for addr, amt in list(_token_balances.items()):
                if addr == "CRO":
                    continue
                if amt and amt > EPSILON:
                    watch_contracts.append(addr)

            # Για κάθε contract βρες top pair & %24h
            for addr in watch_contracts:
                pmeta = None
                pairs = fetch_token_pairs("cronos", addr)
                if pairs:
                    pmeta = pairs[0]
                else:
                    # fallback με symbol
                    sym = (_token_meta.get(addr, {}) or {}).get("symbol") or addr[:8]
                    pmeta = _find_top_pair_for_symbol(sym)
                if not pmeta:
                    continue

                pair_addr = pmeta.get("pairAddress")
                if not pair_addr:
                    continue

                change = pmeta.get("priceChange") or {}
                price  = float(pmeta.get("priceUsd") or 0.0)
                base   = (pmeta.get("baseToken") or {})
                quote  = (pmeta.get("quoteToken") or {})
                sym    = base.get("symbol") or addr[:8]
                quote_sym = quote.get("symbol") or "?"

                # κόψε alerts όταν price == 0 (dead)
                if price <= 0:
                    continue

                ch24 = None
                try:
                    ch24 = float(change.get("h24") or 0.0)
                except Exception:
                    ch24 = 0.0

                key_dump = f"ALRT24_DUMP:{pair_addr}"
                key_pump = f"ALRT24_PUMP:{pair_addr}"

                if ch24 is not None and ch24 <= DUMP_ALERT_24H_PCT:
                    if _alert_gate(key_dump, ALERT_COOLDOWN_MIN):
                        send_telegram(
                            f"⚠️ Dump Alert {sym}/{quote_sym} 24h {ch24:.2f}%\n"
                            f"Price ${_format_price(price)}\n{_pair_link(pair_addr)}"
                        )

                if ch24 is not None and ch24 >= PUMP_ALERT_24H_PCT:
                    if _alert_gate(key_pump, ALERT_COOLDOWN_MIN):
                        send_telegram(
                            f"🚀 Pump Alert {sym}/{quote_sym} 24h {ch24:.2f}%\n"
                            f"Price ${_format_price(price)}\n{_pair_link(pair_addr)}"
                        )

        except Exception as e:
            log.exception("alerts_monitor_loop error: %s", e)

        for _ in range(ALERTS_INTERVAL_MIN * 60):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ----------------------- Guard monitor (short-term after BUY) -----------------------
def _guard_fetch_live_price(addr_or_symbol: str):
    # Προσπάθησε contract-first
    if addr_or_symbol and addr_or_symbol.startswith("0x") and len(addr_or_symbol)==42:
        p = get_token_price("cronos", addr_or_symbol) or 0.0
        if p: return p
        return get_price_usd(addr_or_symbol) or 0.0
    else:
        return get_price_usd(addr_or_symbol) or 0.0

def guard_monitor_loop():
    send_telegram(f"🛡 Guard monitor active: {GUARD_WINDOW_MIN}m window, alert below {GUARD_DUMP_PCT:.1f}% / above {GUARD_PUMP_PCT:.1f}% / trailing {GUARD_TRAILING_DROP_PCT:.1f}%.")
    while not shutdown_event.is_set():
        try:
            now_ts = time.time()
            to_remove = []
            for key, st in list(_guard_state.items()):
                entry = st.get("entry_price", 0.0)
                start = st.get("entry_ts", 0.0)
                peak  = st.get("peak_price", entry)

                if now_ts - start > GUARD_WINDOW_MIN * 60:
                    to_remove.append(key)
                    continue

                live = _guard_fetch_live_price(key)
                if not live or live <= 0:
                    continue

                # update peak
                if live > peak:
                    st["peak_price"] = live
                    peak = live

                # % από entry
                if entry and entry > 0:
                    change_from_entry = (live - entry) / entry * 100.0
                    if change_from_entry >= GUARD_PUMP_PCT:
                        if _alert_gate(f"GUARD_PUMP:{key}", 15):
                            send_telegram(f"🟢 GUARD Pump {key} {change_from_entry:.2f}% from entry (${_format_price(entry)} → ${_format_price(live)})")
                    if change_from_entry <= GUARD_DUMP_PCT:
                        if _alert_gate(f"GUARD_DUMP:{key}", 15):
                            send_telegram(f"🔴 GUARD Dump {key} {change_from_entry:.2f}% from entry (${_format_price(entry)} → ${_format_price(live)})")

                # trailing από peak
                if peak and peak > 0:
                    drop_from_peak = (live - peak) / peak * 100.0
                    if drop_from_peak <= GUARD_TRAILING_DROP_PCT:
                        if _alert_gate(f"GUARD_TRAIL:{key}", 15):
                            send_telegram(f"🟠 GUARD Trailing {key} {drop_from_peak:.2f}% from peak (${_format_price(peak)} → ${_format_price(live)})")

            for k in to_remove:
                _end_guard(k)

        except Exception as e:
            log.exception("guard_monitor_loop error: %s", e)

        for _ in range(20):
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
    log.info("Alerts interval: %sm | Wallet 24h dump/pump: %s/%s", ALERTS_INTERVAL_MIN, DUMP_ALERT_24H_PCT, PUMP_ALERT_24H_PCT)

    # Load persisted ATHs
    _load_ath()

    # Bootstrap watchlist from history (για να αρχίσουν αμέσως alerts)
    try:
        bootstrap_watchlist_from_history()
    except Exception:
        pass

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
