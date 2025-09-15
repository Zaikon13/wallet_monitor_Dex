#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (baseline v2, sandbox-safe)
Compact, <1000 lines, hardened for restricted environments (no tzdata / limited threads).

Key points:
  • TZ Europe/Athens (ZoneInfo/pytz/fixed-offset fallback)
  • Robust imports with inline fallbacks (utils/telegram/reports)
  • Thread capability probe; **single-shot fallback** if threads unavailable
  • Same commands & scheduler semantics when threads are allowed

Env knobs:
  - TZ=Europe/Athens (default) or TZ_OFFSET=+03:00 (fallback)
  - LOOP_FOREVER=1   -> run background threads (production)
  - DISABLE_THREADS=1 -> force single-shot (tests/sandbox)
  - SEND_ONESHOT=1   -> send one heartbeat/report in single-shot
"""

from __future__ import annotations
import os, sys, time, json, signal, logging, threading
from collections import defaultdict, deque
from datetime import datetime, timedelta, tzinfo
from decimal import Decimal, getcontext
from dotenv import load_dotenv

# ---- Precision ----
getcontext().prec = 28

# ---- Import bootstrap (ensure local packages are importable) ----
try:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    CWD = os.getcwd()
    for p in {BASE_DIR, CWD}:
        if p and p not in sys.path:
            sys.path.insert(0, p)
except Exception:
    pass

# ---- External helpers ----
try:
    from utils.http import safe_get, safe_json  # type: ignore
except Exception:
    # Fallback (only if utils/http.py isn't importable)
    import requests  # type: ignore
    def safe_get(url: str, *, params: dict | None = None, timeout: int = 10, retries: int = 2, backoff: float = 0.6):
        attempt = 0
        while True:
            attempt += 1
            try:
                r = requests.get(url, params=params, timeout=timeout)
                if r.status_code >= 500:
                    raise RuntimeError(str(r.status_code))
                return r
            except Exception:
                if attempt > retries:
                    return None
                time.sleep(backoff * attempt)
    def safe_json(resp):
        if resp is None:
            return None
        try:
            return resp.json()
        except Exception:
            try:
                return json.loads(resp.text)
            except Exception:
                return None

# --- Telegram helper (import with fallback) ---
try:
    from telegram.api import send_telegram  # type: ignore
except Exception:
    import os as _os
    import requests as _req
    def send_telegram(text: str):
        token = _os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat  = _os.getenv("TELEGRAM_CHAT_ID", "")
        if not (token and chat and text):
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        try:
            _req.post(url, json={"chat_id": chat, "text": text, "parse_mode": "MarkdownV2", "disable_web_page_preview": True}, timeout=15)
        except Exception:
            pass

# --- Reports helpers (import with fallback) ---
try:
    from reports.day_report import build_day_report_text as _compose_day_report  # type: ignore
    from reports.ledger import append_ledger, update_cost_basis as ledger_update_cost_basis, replay_cost_basis_over_entries  # type: ignore
    from reports.aggregates import aggregate_per_asset  # type: ignore
except Exception:
    def _compose_day_report(**kwargs):
        return "(reporting unavailable)"
    def append_ledger(*a, **k):
        return None
    def ledger_update_cost_basis(*a, **k):
        return (0, 0, 0)
    def replay_cost_basis_over_entries(*a, **k):
        return {}
    def aggregate_per_asset(*a, **k):
        return []

# ===================== Bootstrap / ENV =====================
load_dotenv()

# Robust timezone loader that works without system tzdata (e.g., Pyodide)
class _FixedOffset(tzinfo):
    def __init__(self, minutes: int, name: str | None = None):
        self._offset = timedelta(minutes=minutes)
        sign = '+' if minutes >= 0 else '-'
        m = abs(minutes)
        self._name = name or f"UTC{sign}{m//60:02d}:{m%60:02d}"
    def utcoffset(self, dt):
        return self._offset
    def tzname(self, dt):
        return self._name
    def dst(self, dt):
        return timedelta(0)


def _parse_offset(s: str) -> int:
    s = (s or "").strip()
    if not s:
        return 180  # default +03:00
    try:
        if s.startswith(('+', '-')) and ':' in s:
            sign = 1 if s[0] == '+' else -1
            hh, mm = s[1:].split(':', 1)
            return sign * (int(hh) * 60 + int(mm))
        # plain hours like "+3" or "3" or "-2"
        return int(float(s)) * 60
    except Exception:
        return 180


def _load_local_tz(tz_name: str):
    # 1) stdlib zoneinfo if tzdata present
    try:
        from zoneinfo import ZoneInfo  # imported here to avoid early failure
        return ZoneInfo(tz_name)
    except Exception:
        pass
    # 2) optional pytz if available
    try:
        import pytz  # type: ignore
        return pytz.timezone(tz_name)
    except Exception:
        pass
    # 3) fixed offset fallback (read TZ_OFFSET or default +03:00)
    off = os.getenv("TZ_OFFSET", "+03:00")
    minutes = _parse_offset(off)
    return _FixedOffset(minutes, name=f"{tz_name} (fixed {minutes:+d}m)")

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = _load_local_tz(TZ)


def now_dt():
    return datetime.now(LOCAL_TZ)

def ymd(dt: datetime | None = None) -> str:
    return (dt or now_dt()).strftime("%Y-%m-%d")

def month_prefix(dt: datetime | None = None) -> str:
    return (dt or now_dt()).strftime("%Y-%m")

# Config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS", "")).lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API", "")
CRONOS_RPC_URL     = os.getenv("CRONOS_RPC_URL", "")

DATA_DIR           = os.getenv("DATA_DIR", "/app/data")
OFFSET_PATH        = os.path.join(DATA_DIR, "telegram_offset.json")
ATH_PATH           = os.path.join(DATA_DIR, "ath.json")

# Sane defaults
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
INTRADAY_HOURS     = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR           = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE         = int(os.getenv("EOD_MINUTE", "59"))
RECEIPT_SYMBOLS    = set([s.strip().upper() for s in os.getenv("RECEIPT_SYMBOLS", "TCRO").split(",") if s.strip()])

# Dexscreener endpoints
DEX_BASE_TOKENS    = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH    = "https://api.dexscreener.com/latest/dex/search"

# Etherscan (Cronos)
ETHERSCAN_V2_URL   = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID     = 25
CRONOS_TX_URL      = "https://cronoscan.com/tx/{txhash}"

# Logging
os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("sentinel")

# ===================== Runtime State & Locks =====================
shutdown_event = threading.Event()

BAL_LOCK   = threading.RLock()     # _token_balances/_meta
POS_LOCK   = threading.RLock()     # _position_qty/_position_cost
SEEN_LOCK  = threading.RLock()     # dedupe sets

_token_balances: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))    # key: "CRO" or 0x..
_token_meta: dict[str, dict]        = {}                                     # key -> {symbol, decimals}

_position_qty: dict[str, Decimal]   = defaultdict(lambda: Decimal("0"))     # open qty per key
_position_cost: dict[str, Decimal]  = defaultdict(lambda: Decimal("0"))     # open cost per key (USD)

_seen_tx_hashes: set[str]           = set()                                  # dedupe native hashes
_seen_token_events: deque           = deque(maxlen=4000)                     # dedupe erc20 (tuples)
_seen_token_hashes: deque           = deque(maxlen=2000)

PRICE_CACHE: dict[str, tuple[float | None, float]] = {}
PRICE_CACHE_TTL = 60  # seconds

ATH: dict[str, float] = {}

EPSILON = Decimal("1e-12")

# ===================== File helpers =====================

def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def data_file_for_today() -> str:
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

# ===================== ATH (optional) =====================

def load_ath():
    global ATH
    ATH = read_json(ATH_PATH, default={})
    if not isinstance(ATH, dict):
        ATH = {}


def save_ath():
    write_json(ATH_PATH, ATH)


def update_ath(key: str, live_price: float | None):
    if not live_price or live_price <= 0:
        return
    prev = ATH.get(key)
    if prev is None or live_price > prev + 1e-12:
        ATH[key] = float(live_price)
        save_ath()
        send_telegram(f"🏆 New ATH {key}: ${live_price:.6f}")

# ===================== Pricing (Dexscreener) =====================

CANONICAL_WCRO_QUERIES = ["wcro usdt", "wcro usdc"]

_HISTORY_LAST_PRICE: dict[str, float] = {}


def _pick_best_price(pairs: list[dict]) -> float | None:
    if not pairs:
        return None
    best_price, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId", "")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq, best_price = liq, price
        except Exception:
            continue
    return best_price


def _pairs_for_token_addr(addr: str) -> list[dict]:
    data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/cronos/{addr}", timeout=10, retries=2)) or {}
    pairs = data.get("pairs") or []
    if not pairs:
        data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": addr}, timeout=10, retries=2)) or {}
        pairs = data.get("pairs") or []
    return pairs


def _history_price_fallback(key: str, symbol_hint: str | None = None) -> float | None:
    if not key:
        return None
    k = key.strip()
    if not k:
        return None
    if k.startswith("0x"):
        p = _HISTORY_LAST_PRICE.get(k)
        if p and p > 0:
            return p
    sym = (symbol_hint or k).upper()
    p = _HISTORY_LAST_PRICE.get(sym)
    if p and p > 0:
        return p
    return None


def _price_cro_canonical() -> float | None:
    for q in CANONICAL_WCRO_QUERIES:
        data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10, retries=2)) or {}
        p = _pick_best_price(data.get("pairs"))
        if p and p > 0:
            return p
    return None


def get_price_usd(symbol_or_addr: str) -> float | None:
    if not symbol_or_addr:
        return None
    key = symbol_or_addr.strip().lower()
    now = time.time()
    c = PRICE_CACHE.get(key)
    if c and (now - c[1] < PRICE_CACHE_TTL):
        return c[0]

    price = None
    try:
        if key in ("cro", "wcro", "w-cro", "wrappedcro", "wrapped cro"):
            price = _price_cro_canonical()
            if not price:
                data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": "cro usdt"}, timeout=10, retries=2)) or {}
                price = _pick_best_price(data.get("pairs"))
        elif key.startswith("0x") and len(key) == 42:
            price = _pick_best_price(_pairs_for_token_addr(key))
        else:
            data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": key}, timeout=10, retries=2)) or {}
            price = _pick_best_price(data.get("pairs"))
            if not price and len(key) <= 12:
                for q in (f"{key} usdt", f"{key} wcro"):
                    data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10, retries=2)) or {}
                    price = _pick_best_price(data.get("pairs"))
                    if price:
                        break
    except Exception:
        price = None

    if (price is None) or (not price) or (float(price) <= 0):
        hist = _history_price_fallback(symbol_or_addr, symbol_hint=symbol_or_addr)
        if hist and hist > 0:
            price = float(hist)

    PRICE_CACHE[key] = (price, now)
    return price


def get_change_and_price_for_symbol_or_addr(sym_or_addr: str):
    """Return (price, ch24, ch2h, url). CRO via canonical WCRO route."""
    q = (sym_or_addr or "").strip()
    if not q:
        return (None, None, None, None)
    if q.lower() in ("cro", "wcro"):
        p = _price_cro_canonical()
        if not p:
            return (None, None, None, None)
        return (p, None, None, None)

    if q.lower().startswith("0x") and len(q) == 42:
        pairs = _pairs_for_token_addr(q)
    else:
        data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10, retries=2)) or {}
        pairs = data.get("pairs") or []

    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId", "")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq, best = liq, p
        except Exception:
            continue
    if not best:
        return (None, None, None, None)
    price = float(best.get("priceUsd") or 0)
    ch = best.get("priceChange") or {}
    ch24 = ch.get("h24"); ch2h = ch.get("h2")
    try:
        ch24 = float(ch24) if ch24 is not None else None
        ch2h = float(ch2h) if ch2h is not None else None
    except Exception:
        ch24 = ch2h = None
    ds_url = f"https://dexscreener.com/cronos/{best.get('pairAddress')}"
    return (price, ch24, ch2h, ds_url)

# ===================== Etherscan fetchers =====================

def fetch_latest_wallet_txs(limit: int = 25) -> list[dict]:
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
    r = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=2)
    data = safe_json(r) or {}
    if str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []


def fetch_latest_token_txs(limit: int = 50) -> list[dict]:
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
    r = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=2)
    data = safe_json(r) or {}
    if str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

# ===================== RPC (optional/minimal) =====================
WEB3 = None


def rpc_init() -> bool:
    global WEB3
    if not CRONOS_RPC_URL:
        return False
    try:
        from web3 import Web3
        WEB3 = Web3(Web3.HTTPProvider(CRONOS_RPC_URL, request_kwargs={"timeout": 15}))
        return bool(WEB3.is_connected())
    except Exception as e:
        log.warning("web3 init error: %s", e)
        return False


def rpc_get_native_balance(addr: str) -> float:
    try:
        wei = WEB3.eth.get_balance(addr)
        return float(wei) / (10 ** 18)
    except Exception:
        return 0.0


def rpc_get_erc20_balance(contract: str, owner: str) -> float:
    try:
        from web3 import Web3
        abi = [
            {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
            {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
            {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
        ]
        c = WEB3.eth.contract(address=Web3.to_checksum_address(contract), abi=abi)
        bal = c.functions.balanceOf(Web3.to_checksum_address(owner)).call()
        dec = int(c.functions.decimals().call())
        sym = c.functions.symbol().call()
        with BAL_LOCK:
            _token_meta[contract] = {"symbol": sym, "decimals": dec}
        return float(bal) / (10 ** dec)
    except Exception:
        return 0.0

# ===================== History maps & positions =====================

def _build_history_maps() -> dict[str, str]:
    """Map symbol->contract from past day files + capture last prices."""
    sym2addr: dict[str, str] = {}
    conflict: set[str] = set()
    files = []
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"):
                files.append(fn)
    except Exception:
        pass
    files.sort()
    for fn in files:
        data = read_json(os.path.join(DATA_DIR, fn), default=None)
        if not isinstance(data, dict):
            continue
        for e in data.get("entries", []):
            sym = (e.get("token") or "").strip().upper()
            addr = (e.get("token_addr") or "").strip().lower()
            p = float(e.get("price_usd") or 0.0)
            if p > 0:
                if addr and addr.startswith("0x"):
                    _HISTORY_LAST_PRICE[addr] = p
                if sym:
                    _HISTORY_LAST_PRICE[sym] = p
            if sym and addr and addr.startswith("0x"):
                if sym in sym2addr and sym2addr[sym] != addr:
                    conflict.add(sym)
                else:
                    sym2addr.setdefault(sym, addr)
    for s in conflict:
        sym2addr.pop(s, None)
    return sym2addr


def rebuild_open_positions_from_history():
    pos_qty: dict[str, float] = defaultdict(float)
    pos_cost: dict[str, float] = defaultdict(float)
    sym2addr = _build_history_maps()

    def _update(key: str, signed_amount: float, price_usd: float):
        qty = pos_qty[key]
        cost = pos_cost[key]
        if signed_amount > 0:
            pos_qty[key] = qty + signed_amount
            pos_cost[key] = cost + signed_amount * (price_usd or 0.0)
        elif signed_amount < 0 and qty > 0:
            sell = min(-signed_amount, qty)
            avg = (cost / qty) if qty > 0 else (price_usd or 0.0)
            pos_qty[key] = qty - sell
            pos_cost[key] = max(0.0, cost - avg * sell)

    files = []
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"):
                files.append(fn)
    except Exception:
        pass
    files.sort()
    for fn in files:
        data = read_json(os.path.join(DATA_DIR, fn), default=None)
        if not isinstance(data, dict):
            continue
        for e in data.get("entries", []):
            sym = (e.get("token") or "").strip().upper()
            addr = (e.get("token_addr") or "").strip().lower()
            amt = float(e.get("amount") or 0.0)
            pr = float(e.get("price_usd") or 0.0)
            key = addr if (addr and addr.startswith("0x")) else (sym or "?")
            # Keep TCRO separate (receipt)
            _update(key, amt, pr)

    for k, v in list(pos_qty.items()):
        if abs(v) < 1e-10:
            pos_qty[k] = 0.0
    return pos_qty, pos_cost


def compute_holdings_usd_from_history_positions():
    pos_qty, pos_cost = rebuild_open_positions_from_history()
    total, unreal, breakdown = 0.0, 0.0, []

    def _price_for(key: str, sym_hint: str) -> float:
        p = None
        if key.startswith("0x"):
            p = get_price_usd(key)
        else:
            p = get_price_usd(sym_hint)
        if not p or p <= 0:
            p = _history_price_fallback(key if key.startswith("0x") else sym_hint, symbol_hint=sym_hint) or 0.0
        return float(p or 0.0)

    for key, amt in pos_qty.items():
        amt = max(0.0, float(amt))
        if amt <= 1e-12:
            continue
        sym = key if not key.startswith("0x") else (_token_meta.get(key, {}).get("symbol") or key[:8].upper())
        p = _price_for(key, sym)
        v = amt * p
        total += v
        breakdown.append({"token": sym, "token_addr": key if key.startswith("0x") else None, "amount": amt, "price_usd": p, "usd_value": v})
        cost = pos_cost.get(key, 0.0)
        if amt > 1e-12 and p > 0:
            unreal += (amt * p - cost)

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    return total, breakdown, unreal


def compute_holdings_usd_via_rpc():
    total, breakdown, unreal = 0.0, [], 0.0
    # CRO native
    if rpc_init() and WALLET_ADDRESS:
        try:
            cro_amt = rpc_get_native_balance(WALLET_ADDRESS)
        except Exception:
            cro_amt = 0.0
        if cro_amt > 1e-12:
            cro_price = get_price_usd("CRO") or 0.0
            cro_val = cro_amt * cro_price
            total += cro_val
            breakdown.append({"token": "CRO", "token_addr": None, "amount": cro_amt, "price_usd": cro_price, "usd_value": cro_val})
            with POS_LOCK:
                rem_qty = float(_position_qty.get("CRO", Decimal("0")))
                rem_cost = float(_position_cost.get("CRO", Decimal("0")))
            if rem_qty > 1e-12 and cro_price > 0:
                unreal += (cro_amt * cro_price - rem_cost)
    # Token balances by history symbols/contracts (lightweight)
    sym2addr = _build_history_maps()
    for sym, addr in sym2addr.items():
        try:
            bal = rpc_get_erc20_balance(addr, WALLET_ADDRESS) if rpc_init() else 0.0
        except Exception:
            bal = 0.0
        if bal <= 1e-12:
            continue
        pr = get_price_usd(addr) or 0.0
        val = bal * pr
        total += val
        breakdown.append({"token": sym, "token_addr": addr, "amount": bal, "price_usd": pr, "usd_value": val})
        with POS_LOCK:
            rem_qty = float(_position_qty.get(addr, Decimal("0")))
            rem_cost = float(_position_cost.get(addr, Decimal("0")))
        if rem_qty > 1e-12 and pr > 0:
            unreal += (bal * pr - rem_cost)

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    return total, breakdown, unreal


def compute_holdings_merged():
    """Merge RPC + History. RPC quantity wins; receipts excluded from CRO totals."""
    try:
        total_r, br_r, unrl_r = compute_holdings_usd_via_rpc()
    except Exception:
        total_r, br_r, unrl_r = 0.0, [], 0.0
    try:
        total_h, br_h, unrl_h = compute_holdings_usd_from_history_positions()
    except Exception:
        total_h, br_h, unrl_h = 0.0, [], 0.0

    def _key(b: dict) -> str:
        addr = b.get("token_addr")
        sym = (b.get("token") or "").upper()
        return addr.lower() if (isinstance(addr, str) and addr.startswith("0x")) else sym

    merged: dict[str, dict] = {}
    for b in br_h:
        k = _key(b); merged.setdefault(k, {"hist": None, "rpc": None}); merged[k]["hist"] = b
    for b in br_r:
        k = _key(b); merged.setdefault(k, {"hist": None, "rpc": None}); merged[k]["rpc"] = b

    receipts, breakdown = [], []
    total = 0.0
    unrealized = (unrl_r or 0.0) + (unrl_h or 0.0)

    for k, pair in merged.items():
        src = pair.get("rpc") or pair.get("hist")
        if not src:
            continue
        token = (src.get("token") or "?").upper()
        addr  = src.get("token_addr")
        qty_rpc = float((pair.get("rpc") or {}).get("amount") or 0.0)
        qty_hist= float((pair.get("hist") or {}).get("amount") or 0.0)
        qty = qty_rpc if qty_rpc > 0 else qty_hist
        price = float((pair.get("rpc") or {}).get("price_usd") or 0.0)
        if price <= 0:
            price = float((pair.get("hist") or {}).get("price_usd") or 0.0)
        if price <= 0:
            query = addr if (isinstance(addr, str) and addr and addr.startswith("0x")) else token
            price = float(get_price_usd(query) or 0.0)
        usd_val = qty * price
        rec = {"token": token, "token_addr": addr if (isinstance(addr, str) and addr and addr.startswith("0x")) else None, "amount": qty, "price_usd": price, "usd_value": usd_val}
        if token in RECEIPT_SYMBOLS or token == "TCRO":
            receipts.append(rec)
            continue
        if token == "CRO":
            rec["token"] = "CRO"
        total += usd_val
        breakdown.append(rec)

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    return total, breakdown, unrealized, receipts

# ===================== /dailysum & totals =====================

def summarize_today_per_asset():
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": []})
    entries = data.get("entries", [])
    agg: dict[str, dict] = {}
    for e in entries:
        sym = (e.get("token") or "?").upper()
        amt = float(e.get("amount") or 0.0)
        usd = float(e.get("usd_value") or 0.0)
        rp  = float(e.get("realized_pnl") or 0.0)
        rec = agg.get(sym) or {"symbol": sym, "flow": 0.0, "real": 0.0}
        rec["flow"] += usd
        rec["real"] += rp
        agg[sym] = rec
    out = []
    for r in agg.values():
        p = get_price_usd(r["symbol"]) or 0.0
        out.append({**r, "price": p})
    out.sort(key=lambda x: abs(x["flow"]), reverse=True)
    return out


def _format_daily_sum_message() -> str:
    per = summarize_today_per_asset()
    if not per:
        return f"🧾 Δεν υπάρχουν σημερινές κινήσεις ({ymd()})."
    tot_real = sum(x["real"] for x in per)
    tot_flow = sum(x["flow"] for x in per)
    lines = [f"*🧾 Daily PnL (Today {ymd()}):*"]
    for r in per:
        base = f"• {r['symbol']}: realized ${r['real']:.2f} | flow ${r['flow']:.2f}"
        if r.get("price"):
            base += f" | price ${r['price']:.6f}"
        lines.append(base)
    lines.append("")
    lines.append(f"*Σύνολο realized σήμερα:* ${tot_real:.2f}")
    lines.append(f"*Σύνολο net flow σήμερα:* ${tot_flow:.2f}")
    return "\n".join(lines)


# Totals helpers

def _iter_ledger_files_for_scope(scope: str):
    files = []
    if scope == "today":
        files = [f"transactions_{ymd()}.json"]
    elif scope == "month":
        pref = month_prefix()
        try:
            for fn in os.listdir(DATA_DIR):
                if fn.startswith(f"transactions_{pref}") and fn.endswith(".json"):
                    files.append(fn)
        except Exception:
            pass
    else:  # all
        try:
            for fn in os.listdir(DATA_DIR):
                if fn.startswith("transactions_") and fn.endswith(".json"):
                    files.append(fn)
        except Exception:
            pass
    files.sort()
    return [os.path.join(DATA_DIR, fn) for fn in files]


def _load_entries_for_totals(scope: str):
    entries = []
    for path in _iter_ledger_files_for_scope(scope):
        data = read_json(path, default=None)
        if not isinstance(data, dict):
            continue
        for e in data.get("entries", []):
            sym = (e.get("token") or "?").upper()
            amt = float(e.get("amount") or 0.0)
            usd = float(e.get("usd_value") or 0.0)
            realized = float(e.get("realized_pnl") or 0.0)
            side = "IN" if amt > 0 else "OUT"
            entries.append({"asset": sym, "side": side, "qty": abs(amt), "usd": usd, "realized_usd": realized})
    return entries


def format_totals(scope: str) -> str:
    scope = scope or "all"
    rows = aggregate_per_asset(_load_entries_for_totals(scope))
    if not rows:
        return f"📊 Totals per Asset — {scope.capitalize()}: (no data)"
    lines = [f"📊 Totals per Asset — {scope.capitalize()}:"]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i}. {r['asset']}  "
            f"IN: {r['in_qty']:.6f} (${r['in_usd']:.2f}) | "
            f"OUT: {r['out_qty']:.6f} (${r['out_usd']:.2f}) | "
            f"NET: {r['net_qty']:.6f} (${r['net_usd']:.2f}) | "
            f"REAL: ${r['realized_usd']:.2f}  TX: {r['tx_count']}"
        )
    return "\n".join(lines)

# ===================== Telegram long poll =====================

def _load_offset() -> int:
    d = read_json(OFFSET_PATH, default={})
    try:
        return int(d.get("offset", 0))
    except Exception:
        return 0


def _save_offset(off: int):
    try:
        write_json(OFFSET_PATH, {"offset": int(off)})
    except Exception:
        pass


def _tg_get_updates(offset: int, timeout: int = 50):
    import requests
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": timeout, "offset": offset, "allowed_updates": json.dumps(["message"])}
    try:
        r = requests.get(url, params=params, timeout=timeout + 5)
        data = r.json()
        return data.get("result", []) if isinstance(data, dict) else []
    except Exception:
        return []


def _handle_command(text: str):
    t = (text or "").strip()
    low = t.lower()

    if low.startswith("/status"):
        total, br, unrl, rec = compute_holdings_merged()
        msg = [f"*Status*", "", f"Holdings: ${total:.2f} | Unreal: ${unrl:.2f}"]
        for b in br[:12]:
            msg.append(f"• {b['token']}: {b['amount']:.6f} = ${b['usd_value']:.2f}")
        if rec:
            msg.append("")
            msg.append("Receipts:")
            for r in rec[:6]:
                msg.append(f"• {r['token']}: {r['amount']:.6f}")
        send_telegram("\n".join(msg))
        return

    if low.startswith("/holdings") or low.startswith("/show") or low.startswith("/show_wallet_assets") or low.startswith("/showwalletassets"):
        total, br, unrl, rec = compute_holdings_merged()
        msg = [f"*Holdings (merged)* ${total:.2f}"]
        for b in br:
            msg.append(f"• {b['token']}: {b['amount']:.6f} @ ${b['price_usd']:.6f} = ${b['usd_value']:.2f}")
        if rec:
            msg.append("")
            msg.append("Receipts:")
            for r in rec:
                msg.append(f"• {r['token']}: {r['amount']:.6f}")
        if unrl := unrl:
            msg.append("")
            msg.append(f"Unrealized: ${unrl:.2f}")
        send_telegram("\n".join(msg))
        return

    if low.startswith("/dailysum") or low.startswith("/showdaily"):
        send_telegram(_format_daily_sum_message())
        return

    if low.startswith("/totalsmonth"):
        send_telegram(format_totals("month"))
        return

    if low.startswith("/totalstoday"):
        send_telegram(format_totals("today"))
        return

    if low.startswith("/totals"):
        send_telegram(format_totals("all"))
        return

    if low.startswith("/report"):
        date_str = ymd()
        path = data_file_for_today()
        data = read_json(path, default={"date": date_str, "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        entries = data.get("entries", [])
        net_flow = float(data.get("net_usd_flow", 0.0))
        realized_today_total = float(data.get("realized_pnl", 0.0))
        holdings_total, breakdown, unrealized, _receipts = compute_holdings_merged()
        text = _compose_day_report(
            date_str=date_str,
            entries=entries,
            net_flow=net_flow,
            realized_today_total=realized_today_total,
            holdings_total=holdings_total,
            breakdown=breakdown,
            unrealized=unrealized,
            data_dir=DATA_DIR,
        )
        send_telegram(text)
        return

    if low.startswith("/rescan"):
        # Placeholder: on baseline we just acknowledge; advanced flows can refresh caches
        send_telegram("🔄 Rescan initialized (refreshing caches on next loops).")
        return

    send_telegram("❓ Άγνωστη εντολή. Διαθέσιμες: /status /holdings /show /dailysum /totals /totalstoday /totalsmonth /report /rescan")


def telegram_long_poll_loop():
    offset = _load_offset()
    backoff = 1.0
    while not shutdown_event.is_set():
        try:
            updates = _tg_get_updates(offset=offset, timeout=50)
            max_id = offset
            for u in updates:
                try:
                    uid = int(u.get("update_id"))
                    msg = (u.get("message") or {})
                    chat_id = str(((msg.get("chat") or {}).get("id")) or "")
                    if TELEGRAM_CHAT_ID and chat_id and chat_id != TELEGRAM_CHAT_ID:
                        max_id = max(max_id, uid)
                        continue
                    text = (msg.get("text") or "").strip()
                    if text:
                        _handle_command(text)
                    max_id = max(max_id, uid)
                except Exception:
                    continue
            if max_id != offset:
                offset = max_id + 1
                _save_offset(offset)
            backoff = 1.0
        except Exception:
            time.sleep(backoff)
            backoff = min(backoff * 2, 8.0)

# ===================== Thread probe =====================

def _thread_supported() -> bool:
    if os.getenv("DISABLE_THREADS", "").lower() in ("1", "true", "yes"):
        return False
    try:
        t = threading.Thread(target=lambda: None, daemon=True)
        t.start(); t.join()
        return True
    except Exception:
        return False

# ===================== Scheduler & main =====================

def _scheduler_loop():
    last_intraday = 0.0
    last_eod_date = ""
    while not shutdown_event.is_set():
        dt = now_dt()
        # Intraday
        if INTRADAY_HOURS > 0 and (time.time() - last_intraday) >= INTRADAY_HOURS * 3600:
            try:
                send_telegram("⏱ Intraday report…")
                _handle_command("/report")
            except Exception:
                pass
            last_intraday = time.time()
        # EOD
        if dt.strftime("%H:%M") == f"{EOD_HOUR:02d}:{EOD_MINUTE:02d}" and last_eod_date != ymd(dt):
            try:
                send_telegram("🌙 End-of-day report…")
                _handle_command("/report")
            except Exception:
                pass
            last_eod_date = ymd(dt)
        for _ in range(10):
            if shutdown_event.is_set():
                break
            time.sleep(6)


def _single_shot_tick():
    # Minimal work on single-shot to prove liveness without threads.
    if os.getenv("SEND_ONESHOT", "0") == "1":
        try:
            send_telegram(f"🫡 Heartbeat {now_dt():%Y-%m-%d %H:%M:%S}")
        except Exception:
            pass


def _graceful_exit(*_):
    try:
        shutdown_event.set()
    except Exception:
        pass


def main():
    load_ath()
    send_telegram("✅ Sentinel started (baseline v2, sandbox-safe).")

    # If threads not supported or not requested, run single-shot and exit
    if not _thread_supported() or os.getenv("LOOP_FOREVER", "0") != "1":
        _single_shot_tick()
        return

    # Threads available — run poller + scheduler
    try:
        th_poll = threading.Thread(target=telegram_long_poll_loop, name="tg-poll", daemon=False)
        th_sched = threading.Thread(target=_scheduler_loop, name="scheduler", daemon=False)
        th_poll.start(); th_sched.start()
    except Exception as e:
        # Harden against: RuntimeError: can't start new thread
        logging.warning("thread start failed (%s); falling back to single-shot", e)
        _single_shot_tick()
        return

    try:
        while th_poll.is_alive() or th_sched.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _graceful_exit()
    finally:
        try:
            th_poll.join(timeout=5)
            th_sched.join(timeout=5)
        except Exception:
            pass


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    main()

# --- EOF (baseline v2). Keep main <1000 lines. ---
