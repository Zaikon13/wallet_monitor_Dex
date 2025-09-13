#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — SINGLE-FILE MAIN
- RPC snapshot (CRO + ERC20)
- Dexscreener pricing (HTTP helper), history fallback
- Cost-basis PnL (realized & unrealized)
- Intraday / EOD reports
- Telegram commands (long-poll)
- Robust shutdown tracing

Requires modules:
  utils/http.py              -> safe_get, safe_json
  telegram/api.py            -> send_telegram
  reports/day_report.py      -> build_day_report_text
  reports/ledger.py          -> append_ledger, update_cost_basis, replay_cost_basis_over_entries
  reports/aggregates.py      -> aggregate_per_asset
"""

from __future__ import annotations

import os
import sys
import time
import json
import signal
import random
import logging
import threading
from collections import deque, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ---- externalized helpers
from utils.http import safe_get, safe_json
from telegram.api import send_telegram
from reports.day_report import build_day_report_text as _compose_day_report
from reports.ledger import (
    append_ledger,
    update_cost_basis as ledger_update_cost_basis,
    replay_cost_basis_over_entries,
)
from reports.aggregates import aggregate_per_asset

# ------------------------------------------------------------
# Bootstrap / Config
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wallet-monitor")

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = ZoneInfo(TZ)

def now_dt():
    return datetime.now(LOCAL_TZ)

def ymd(dt=None):
    if dt is None:
        dt = now_dt()
    return dt.strftime("%Y-%m-%d")

def month_prefix(dt=None):
    if dt is None:
        dt = now_dt()
    return dt.strftime("%Y-%m")

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API") or ""
CRONOS_RPC_URL     = os.getenv("CRONOS_RPC_URL") or ""

# Dexscreener endpoints
DEX_BASE_PAIRS  = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH = "https://api.dexscreener.com/latest/dex/search"

# Receipts (do not add to MTM total)
RECEIPT_SYMBOLS = set([s.strip().upper() for s in os.getenv("RECEIPT_SYMBOLS", "TCRO").split(",") if s.strip()])

# Polls
WALLET_POLL = int(os.getenv("WALLET_POLL", "12"))

# eps
EPSILON = 1e-12

# runtime state
shutdown_event = threading.Event()

# ------------------------------------------------------------
# Shutdown tracing helper
# ------------------------------------------------------------
import traceback
def trigger_shutdown(reason: str):
    try:
        log.error(">>> SHUTDOWN TRIGGERED: %s", reason)
        log.error("Stack:\n%s", "".join(traceback.format_stack()))
    except Exception:
        pass
    shutdown_event.set()

# ------------------------------------------------------------
# Data-files
# ------------------------------------------------------------
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

def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

# ------------------------------------------------------------
# Minimal Web3 (optional; used only for balances)
# ------------------------------------------------------------
WEB3 = None
ERC20_ABI_MIN = [
    {"constant": True, "inputs": [], "name": "symbol",   "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}],  "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

def _to_checksum(addr: str):
    try:
        from web3 import Web3
        return Web3.to_checksum_address(addr)
    except Exception:
        return addr

def rpc_init():
    global WEB3
    if not CRONOS_RPC_URL:
        return False
    if WEB3 is not None:
        return True
    try:
        from web3 import Web3
        WEB3 = Web3(Web3.HTTPProvider(CRONOS_RPC_URL, request_kwargs={"timeout": 15}))
        return WEB3.is_connected()
    except Exception as e:
        log.warning("web3 init error: %s", e)
        WEB3 = None
        return False

def rpc_get_native_balance(addr: str) -> float:
    try:
        if not rpc_init():
            return 0.0
        wei = WEB3.eth.get_balance(_to_checksum(addr))
        return float(wei) / (10 ** 18)
    except Exception:
        return 0.0

def rpc_get_erc20_balance(contract: str, owner: str) -> float:
    try:
        if not rpc_init():
            return 0.0
        c = WEB3.eth.contract(address=_to_checksum(contract), abi=ERC20_ABI_MIN)
        bal = c.functions.balanceOf(_to_checksum(owner)).call()
        dec = int(c.functions.decimals().call())
        return float(bal) / (10 ** dec)
    except Exception:
        return 0.0

# ------------------------------------------------------------
# Pricing via Dexscreener
# ------------------------------------------------------------
PRICE_CACHE = {}
PRICE_CACHE_TTL = 60

def _sanitize_price(p):
    try:
        p = float(p)
        if p < 0:
            return None
        return p
    except Exception:
        return None

def _pick_best_price(pairs):
    if not pairs:
        return None
    best = None
    best_liq = -1.0
    for p in pairs:
        try:
            if str(p.get("chainId", "")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = _sanitize_price(p.get("priceUsd"))
            if not price:
                continue
            if liq > best_liq:
                best_liq = liq
                best = price
        except Exception:
            continue
    return best

def get_price_usd(symbol_or_addr: str):
    if not symbol_or_addr:
        return None
    key = symbol_or_addr.strip()
    key_l = key.lower()
    # cache
    now_ts = time.time()
    c = PRICE_CACHE.get(key_l)
    if c and (now_ts - c[1] < PRICE_CACHE_TTL):
        return c[0]

    price = None
    try:
        if key_l in ("cro", "wcro"):
            # find CRO via wcro/usdt first
            r = safe_get(DEX_BASE_SEARCH, params={"q": "wcro usdt"}, timeout=10)
            data = safe_json(r) or {}
            price = _pick_best_price(data.get("pairs"))
            if not price:
                r = safe_get(DEX_BASE_SEARCH, params={"q": "cro usdt"}, timeout=10)
                data = safe_json(r) or {}
                price = _pick_best_price(data.get("pairs"))
        elif key_l.startswith("0x") and len(key_l) == 42:
            # address
            r = safe_get(f"{DEX_BASE_TOKENS}/cronos/{key_l}", timeout=10)
            data = safe_json(r) or {}
            pairs = data.get("pairs") or []
            if not pairs:
                r = safe_get(DEX_BASE_SEARCH, params={"q": key_l}, timeout=10)
                data = safe_json(r) or {}
                pairs = data.get("pairs") or []
            price = _pick_best_price(pairs)
        else:
            # symbol search
            r = safe_get(DEX_BASE_SEARCH, params={"q": key_l}, timeout=10)
            data = safe_json(r) or {}
            pairs = data.get("pairs") or []
            price = _pick_best_price(pairs)
            if not price:
                r = safe_get(DEX_BASE_SEARCH, params={"q": f"{key_l} usdt"}, timeout=10)
                data = safe_json(r) or {}
                price = _pick_best_price(data.get("pairs") or [])
    except Exception:
        price = None

    if price is not None and price > 0:
        PRICE_CACHE[key_l] = (float(price), now_ts)
        return float(price)

    PRICE_CACHE[key_l] = (None, now_ts)
    return None

def get_change_and_price_for_symbol_or_addr(sym_or_addr: str):
    pairs = []
    try:
        if sym_or_addr.lower().startswith("0x"):
            r = safe_get(f"{DEX_BASE_TOKENS}/cronos/{sym_or_addr}", timeout=10)
            data = safe_json(r) or {}
            pairs = data.get("pairs") or []
        else:
            r = safe_get(DEX_BASE_SEARCH, params={"q": sym_or_addr}, timeout=10)
            data = safe_json(r) or {}
            pairs = data.get("pairs") or []
    except Exception:
        pairs = []
    best = None
    best_liq = -1.0
    for p in pairs:
        try:
            if str(p.get("chainId", "")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = _sanitize_price(p.get("priceUsd"))
            if not price:
                continue
            if liq > best_liq:
                best_liq = liq
                best = p
        except Exception:
            continue
    if not best:
        return (None, None, None, None)
    price = float(best.get("priceUsd") or 0)
    ch24 = None
    ch2h = None
    try:
        ch = best.get("priceChange") or {}
        if "h24" in ch:
            ch24 = float(ch.get("h24"))
        if "h2" in ch:
            ch2h = float(ch.get("h2"))
    except Exception:
        pass
    ds_url = f"https://dexscreener.com/cronos/{best.get('pairAddress')}"
    return (price, ch24, ch2h, ds_url)

# ------------------------------------------------------------
# Runtime state for positions & balances
# ------------------------------------------------------------
_token_balances = defaultdict(float)  # key: "CRO" or contract (0x..) or SYMBOL
_token_meta     = {}                  # key -> {"symbol","decimals"}

_position_qty   = defaultdict(float)  # key: token_addr or SYMBOL
_position_cost  = defaultdict(float)  # accum cost for open qty

# ------------------------------------------------------------
# Helpers: history maps
# ------------------------------------------------------------
_HISTORY_LAST_PRICE = {}

def _build_history_maps():
    symbol_to_contract = {}
    symbol_conflicts = set()
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
            sym = (e.get("token") or "").strip()
            addr = (e.get("token_addr") or "").strip().lower()
            p = float(e.get("price_usd") or 0.0)
            if p > 0:
                if addr and addr.startswith("0x"):
                    _HISTORY_LAST_PRICE[addr] = p
                if sym:
                    _HISTORY_LAST_PRICE[sym.upper()] = p
            if sym and addr and addr.startswith("0x"):
                if sym in symbol_to_contract and symbol_to_contract[sym] != addr:
                    symbol_conflicts.add(sym)
                else:
                    symbol_to_contract.setdefault(sym, addr)
    for s in symbol_conflicts:
        symbol_to_contract.pop(s, None)
    return symbol_to_contract

# ------------------------------------------------------------
# Holdings calculators
# ------------------------------------------------------------
def rebuild_open_positions_from_history():
    """
    Rebuild open positions (qty,cost) purely from history entries across all days.
    Keeps receipt tokens (e.g., TCRO) DISTINCT (no alias to CRO).
    """
    pos_qty = defaultdict(float)
    pos_cost = defaultdict(float)
    symbol_to_contract = _build_history_maps()

    def _update(token_key, signed_amount, price_usd):
        qty = pos_qty[token_key]
        cost = pos_cost[token_key]
        if signed_amount > EPSILON:
            pos_qty[token_key] = qty + signed_amount
            pos_cost[token_key] = cost + signed_amount * (price_usd or 0.0)
        elif signed_amount < -EPSILON and qty > EPSILON:
            sell_qty = min(-signed_amount, qty)
            avg_cost = (cost / qty) if qty > EPSILON else (price_usd or 0.0)
            pos_qty[token_key] = qty - sell_qty
            pos_cost[token_key] = max(0.0, cost - avg_cost * sell_qty)

    files = []
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"):
                files.append(fn)
    except Exception as ex:
        log.warning("listdir data error: %s", ex)

    files.sort()
    for fn in files:
        data = read_json(os.path.join(DATA_DIR, fn), default=None)
        if not isinstance(data, dict):
            continue
        for e in data.get("entries", []):
            sym_raw = (e.get("token") or "").strip()
            addr_raw = (e.get("token_addr") or "").strip().lower()
            amt = float(e.get("amount") or 0.0)
            pr = float(e.get("price_usd") or 0.0)

            # key preference: address else symbol (KEEP tCRO distinct)
            if addr_raw and addr_raw.startswith("0x"):
                key = addr_raw
            else:
                mapped = symbol_to_contract.get(sym_raw) or symbol_to_contract.get(sym_raw.upper())
                key = mapped if (mapped and mapped.startswith("0x")) else (sym_raw.upper() or sym_raw or "?")
            _update(key, amt, pr)

    # zero tiny dust
    for k, v in list(pos_qty.items()):
        if abs(v) < 1e-10:
            pos_qty[k] = 0.0
    return pos_qty, pos_cost

def compute_holdings_usd_from_history_positions():
    pos_qty, pos_cost = rebuild_open_positions_from_history()

    def _sym_for_key(key):
        if isinstance(key, str) and key.startswith("0x"):
            return _token_meta.get(key, {}).get("symbol") or key[:8].upper()
        return str(key)

    def _price_for(key, sym_hint):
        if isinstance(key, str) and key.startswith("0x"):
            p = get_price_usd(key)
        else:
            p = get_price_usd(sym_hint)
        if p is None:
            p = _HISTORY_LAST_PRICE.get(key if (isinstance(key, str) and key.startswith("0x")) else sym_hint) or 0.0
        return float(p or 0.0)

    total = 0.0
    breakdown = []
    unrealized = 0.0
    for key, amt in pos_qty.items():
        amt = max(0.0, float(amt))
        if amt <= EPSILON:
            continue
        sym = _sym_for_key(key)
        p = _price_for(key, sym)
        v = amt * p
        total += v
        breakdown.append({"token": sym, "token_addr": key if (isinstance(key, str) and key.startswith("0x")) else None, "amount": amt, "price_usd": p, "usd_value": v})
        cost = pos_cost.get(key, 0.0)
        if amt > EPSILON and p > 0:
            unrealized += (amt * p - cost)

    # keep TCRO as TCRO (receipt)
    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    return total, breakdown, unrealized

def compute_holdings_usd_via_rpc():
    total = 0.0
    breakdown = []
    unrealized = 0.0

    # CRO native
    cro_amt = 0.0
    try:
        cro_amt = rpc_get_native_balance(WALLET_ADDRESS)
    except Exception:
        cro_amt = 0.0
    if cro_amt > EPSILON:
        cro_price = get_price_usd("CRO") or 0.0
        cro_val = cro_amt * cro_price
        total += cro_val
        breakdown.append({"token": "CRO", "token_addr": None, "amount": cro_amt, "price_usd": cro_price, "usd_value": cro_val})

    # any known contracts from history map
    symbol_to_contract = _build_history_maps()
    for addr in sorted(set(symbol_to_contract.values())):
        if not addr or not addr.startswith("0x"):
            continue
        try:
            bal = rpc_get_erc20_balance(addr, WALLET_ADDRESS)
            if bal <= EPSILON:
                continue
            pr = get_price_usd(addr) or 0.0
            sym = _token_meta.get(addr, {}).get("symbol") or "TOKEN"
            val = bal * pr
            total += val
            breakdown.append({"token": sym, "token_addr": addr, "amount": bal, "price_usd": pr, "usd_value": val})
        except Exception:
            continue

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    return total, breakdown, unrealized

def _recompute_unreal_from_merged(breakdown_list):
    # Unrealized from merged breakdown: open qty valued at live price minus accumulated historical cost
    # We rebuild positions from ALL history once, then compute diff per asset present in breakdown.
    pos_qty, pos_cost = rebuild_open_positions_from_history()
    unreal = 0.0
    for b in breakdown_list:
        amt = float(b.get("amount") or 0.0)
        prc = float(b.get("price_usd") or 0.0)
        if amt <= EPSILON or prc <= 0:
            continue
        key = b.get("token_addr") if (isinstance(b.get("token_addr"), str) and b.get("token_addr", "").startswith("0x")) else (b.get("token") or "").upper()
        open_qty = float(pos_qty.get(key) or 0.0)
        open_cost = float(pos_cost.get(key) or 0.0)
        if open_qty > EPSILON:
            unreal += (open_qty * prc - open_cost)
    return unreal

def compute_holdings_merged():
    """
    Merge RPC + History. Returns:
      total_usd, breakdown(list), unrealized_usd, receipts(list)
    - 'breakdown' excludes receipt symbols (e.g., TCRO) from MTM total
    - 'receipts' holds them separately (price usually 0)
    - CRO line present if qty>0 from any source; or 0-qty if activity today or SHOW_ZERO_CRO_LINE=1
    """
    total_r, br_r, _ = compute_holdings_usd_via_rpc()
    total_h, br_h, _ = compute_holdings_usd_from_history_positions()

    def _key(b):
        addr = b.get("token_addr")
        sym = (b.get("token") or "").upper()
        return addr.lower() if (isinstance(addr, str) and addr.startswith("0x")) else sym

    merged = {}
    def _add(b):
        k = _key(b)
        if not k:
            return
        cur = merged.get(k, {"token": b.get("token"), "token_addr": b.get("token_addr"), "amount": 0.0, "price_usd": 0.0, "usd_value": 0.0})
        if not cur.get("token"):
            cur["token"] = b.get("token")
        if not cur.get("token_addr") and b.get("token_addr"):
            cur["token_addr"] = b.get("token_addr")
        cur["amount"] += float(b.get("amount") or 0.0)
        pr = float(b.get("price_usd") or 0.0)
        if pr > 0:
            cur["price_usd"] = pr
        merged[k] = cur

    for b in br_h or []:
        _add(b)
    for b in br_r or []:
        _add(b)

    def _cro_qty() -> float:
        for v in merged.values():
            if (v.get("token") or "").upper() == "CRO":
                return float(v.get("amount") or 0.0)
        return 0.0

    def _had_cro_activity_today() -> bool:
        try:
            d = read_json(data_file_for_today(), default={"entries": []})
            for e in d.get("entries", []):
                if (e.get("token") or "").strip().upper() == "CRO":
                    return True
        except Exception:
            pass
        return False

    if _cro_qty() <= EPSILON:
        # source CRO from rpc/history/runtime
        cro_amt = 0.0
        try:
            cro_amt = max(cro_amt, float(rpc_get_native_balance(WALLET_ADDRESS) or 0.0))
        except Exception:
            pass
        if cro_amt <= EPSILON:
            try:
                pos_q, _ = rebuild_open_positions_from_history()
                cro_amt = max(cro_amt, float(pos_q.get("CRO") or 0.0))
            except Exception:
                pass
        if cro_amt <= EPSILON:
            try:
                cro_amt = max(cro_amt, float(_token_balances.get("CRO") or 0.0))
            except Exception:
                pass
        if cro_amt > EPSILON:
            merged["CRO"] = {"token": "CRO", "token_addr": None, "amount": cro_amt, "price_usd": get_price_usd("CRO") or 0.0, "usd_value": 0.0}
        else:
            if (os.getenv("SHOW_ZERO_CRO_LINE", "0").lower() in ("1","true","yes","on")) or _had_cro_activity_today():
                merged["CRO"] = {"token": "CRO", "token_addr": None, "amount": 0.0, "price_usd": get_price_usd("CRO") or 0.0, "usd_value": 0.0}

    receipts = []
    breakdown = []
    total = 0.0
    for rec in merged.values():
        symU = (rec.get("token") or "").upper()
        if symU in RECEIPT_SYMBOLS or symU == "TCRO":
            receipts.append(rec)
            continue
        amt = float(rec.get("amount", 0.0))
        prc = float(rec.get("price_usd", 0.0) or 0.0)
        rec["usd_value"] = amt * prc
        total += rec["usd_value"]
        breakdown.append(rec)

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    unrealized = _recompute_unreal_from_merged(breakdown)
    return total, breakdown, unrealized, receipts

# ------------------------------------------------------------
# Etherscan fetchers
# ------------------------------------------------------------
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25

def fetch_latest_wallet_txs(limit=30):
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
    if str(data.get("status", "")) == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

def fetch_latest_token_txs(limit=60):
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
    if str(data.get("status", "")) == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

# ------------------------------------------------------------
# TX handlers (native/ERC20)
# ------------------------------------------------------------
_seen_native = set()
_seen_token_hashes = set()

def handle_native_tx(tx: dict):
    h = tx.get("hash")
    if not h or h in _seen_native:
        return
    _seen_native.add(h)

    val_raw = tx.get("value", "0")
    try:
        amount_cro = int(val_raw) / 10 ** 18
    except Exception:
        amount_cro = float(val_raw) if val_raw else 0.0

    frm = (tx.get("from") or "").lower()
    to = (tx.get("to") or "").lower()
    ts = int(tx.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(ts, LOCAL_TZ) if ts > 0 else now_dt()
    sign = +1 if to == WALLET_ADDRESS else (-1 if frm == WALLET_ADDRESS else 0)
    if sign == 0 or abs(amount_cro) <= EPSILON:
        return

    price = get_price_usd("CRO") or 0.0
    usd_value = sign * amount_cro * price

    _token_balances["CRO"] += sign * amount_cro
    if abs(_token_balances["CRO"]) < 1e-10:
        _token_balances["CRO"] = 0.0
    _token_meta["CRO"] = {"symbol": "CRO", "decimals": 18}

    realized = ledger_update_cost_basis(_position_qty, _position_cost, "CRO", sign * amount_cro, price, eps=EPSILON)

    link = f"https://cronoscan.com/tx/{h}"
    send_telegram(
        f"Native TX ({'IN' if sign>0 else 'OUT'}) CRO\n"
        f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign * amount_cro:.6f} CRO\n"
        f"Price: ${price:.6f}\n"
        f"USD value: ${usd_value:.6f}"
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
    append_ledger(entry)

def handle_erc20_tx(t: dict):
    h = t.get("hash") or ""
    if h in _seen_token_hashes:
        return
    _seen_token_hashes.add(h)

    frm = (t.get("from") or "").lower()
    to = (t.get("to") or "").lower()
    token_addr = (t.get("contractAddress") or "").lower()
    symbol = t.get("tokenSymbol") or (token_addr[:8] if token_addr else "?")
    try:
        decimals = int(t.get("tokenDecimal") or 18)
    except Exception:
        decimals = 18
    val_raw = t.get("value", "0")
    if WALLET_ADDRESS not in (frm, to):
        return
    try:
        amount = int(val_raw) / (10 ** decimals)
    except Exception:
        amount = float(val_raw) if val_raw else 0.0

    ts = int(t.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(ts, LOCAL_TZ) if ts > 0 else now_dt()
    sign = +1 if to == WALLET_ADDRESS else -1

    # price
    if token_addr and token_addr.startswith("0x") and len(token_addr) == 42:
        price = get_price_usd(token_addr) or 0.0
    else:
        price = get_price_usd(symbol) or 0.0
    usd_value = sign * amount * (price or 0.0)

    key = token_addr if token_addr else symbol.upper()
    _token_balances[key] += sign * amount
    if abs(_token_balances[key]) < 1e-10:
        _token_balances[key] = 0.0
    _token_meta[key] = {"symbol": symbol, "decimals": decimals}

    realized = ledger_update_cost_basis(_position_qty, _position_cost, key, sign * amount, (price or 0.0), eps=EPSILON)

    link = f"https://cronoscan.com/tx/{h}"
    direction = "IN" if sign > 0 else "OUT"
    send_telegram(
        f"Token TX ({direction}) {symbol}\n"
        f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign * amount:.6f} {symbol}\n"
        f"Price: ${price:.6f}\n"
        f"USD value: ${usd_value:.6f}"
    )

    entry = {
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h or None,
        "type": "erc20",
        "token": symbol,
        "token_addr": token_addr or None,
        "amount": sign * amount,
        "price_usd": price or 0.0,
        "usd_value": usd_value,
        "realized_pnl": realized,
        "from": frm,
        "to": to,
    }
    append_ledger(entry)

# ------------------------------------------------------------
# Wallet monitor loop (Etherscan)
# ------------------------------------------------------------
def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    while not shutdown_event.is_set():
        try:
            nat = fetch_latest_wallet_txs(limit=40)
            for tx in nat or []:
                handle_native_tx(tx)
            toks = fetch_latest_token_txs(limit=80)
            for t in toks or []:
                handle_erc20_tx(t)
        except Exception as ex:
            log.warning("wallet monitor error: %s", ex)
        for _ in range(WALLET_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1.0)

# ------------------------------------------------------------
# Daily summary helpers
# ------------------------------------------------------------
def _load_entries_for_totals(scope: str):
    entries = []
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
    else:
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
            sym = (e.get("token") or "?").upper()
            amt = float(e.get("amount") or 0.0)
            usd = float(e.get("usd_value") or 0.0)
            realized = float(e.get("realized_pnl") or 0.0)
            addr = (e.get("token_addr") or "").lower() if (e.get("token_addr") and str(e.get("token_addr")).startswith("0x")) else None
            side = "IN" if amt > 0 else "OUT"
            entries.append({
                "asset": sym,
                "token_addr": addr,
                "side": side,
                "qty": abs(amt),
                "usd": usd,
                "realized_usd": realized,
            })
    return entries

def format_totals(scope: str):
    scope = scope or "all"
    rows = aggregate_per_asset(_load_entries_for_totals(scope), by_addr=True)
    if not rows:
        return f"📊 Totals per Asset — {scope.capitalize()}: (no data)"
    lines = [f"📊 Totals per Asset — {scope.capitalize()}:"]
    for i, r in enumerate(rows, 1):
        name = r["asset"]
        addr = r.get("token_addr")
        if addr:
            short = addr[:8] + "…" + addr[-4:]
            name = f"{name} [{short}]"
        lines.append(
            f"{i}. {name}  "
            f"IN: {r['in_qty']:.6f} (${r['in_usd']:.6f}) | "
            f"OUT: {r['out_qty']:.6f} (${r['out_usd']:.6f}) | "
            f"REAL: ${r['realized_usd']:.6f}"
        )
    tot_real = sum(float(x.get("realized_usd") or 0.0) for x in rows)
    lines.append("")
    lines.append(f"Σύνολο realized: ${tot_real:.6f}")
    return "\n".join(lines)

def format_holdings():
    total, breakdown, unreal, receipts = compute_holdings_merged()
    lines = ["📦 Holdings (merged):"]
    for b in breakdown:
        tok = b.get("token") or "?"
        amt = float(b.get("amount") or 0.0)
        prc = float(b.get("price_usd") or 0.0)
        val = amt * prc
        lines.append(f"• {tok}: {amt:,.4f}  @ ${prc:.6f}  = ${val:,.4f}")
    if receipts:
        lines.append("")
        lines.append("Receipts:")
        for r in receipts:
            tok = r.get("token") or "?"
            amt = float(r.get("amount") or 0.0)
            lines.append(f"• {tok}: {amt:,.4f}")
    lines.append("")
    lines.append(f"Σύνολο: ${sum(float(b.get('amount',0.0))*float(b.get('price_usd',0.0) or 0.0) for b in breakdown):,.4f}")
    if abs(unreal) > 1e-9:
        lines.append(f"Unrealized: ${unreal:,.4f}")
    return "\n".join(lines)

def format_pnl(scope: str):
    """
    Per-asset PnL report for scope today|month|all:
    Buys/Sells (qty, USD), Net qty, Net flow, Realized, Open qty, Live price, Unrealized, Ending value.
    CRO & USDT balances emphasized.
    """
    scope = scope or "today"
    entries = _load_entries_for_totals(scope)
    if not entries:
        return f"🧾 PnL ({scope}): (no data)"
    # aggregate buys/sells/realized by (asset, token_addr)
    rows = aggregate_per_asset(entries, by_addr=True)
    # current holdings for ending positions
    _, merged, _, _ = compute_holdings_merged()
    # map for quick lookup
    idx = {}
    for b in merged:
        k = ((b.get("token") or "").upper(), b.get("token_addr"))
        idx[k] = {"open_qty": float(b.get("amount") or 0.0), "price_now": float(b.get("price_usd") or 0.0)}

    lines = [f"🧾 PnL ({scope}):"]
    cro_line = None
    usdt_line = None
    total_unreal = 0.0
    total_end_val = 0.0

    def _name(asset, addr):
        if addr:
            short = addr[:8] + "…" + addr[-4:]
            return f"{asset} [{short}]"
        return asset

    # order by abs net flow
    rows_sorted = sorted(rows, key=lambda r: abs(float(r["in_usd"]) - abs(float(r["out_usd"]))), reverse=True)
    for r in rows_sorted:
        asset = r["asset"]
        addr  = r.get("token_addr")
        in_qty, in_usd   = float(r["in_qty"]), float(r["in_usd"])
        out_qty, out_usd = float(r["out_qty"]), float(r["out_usd"])
        realized         = float(r["realized_usd"])
        net_qty          = in_qty - out_qty
        net_flow_usd     = in_usd + out_usd  # out_usd is negative

        open_q   = float(idx.get((asset, addr), {}).get("open_qty", 0.0))
        price    = float(idx.get((asset, addr), {}).get("price_now", 0.0))
        unreal   = open_q * price  # minus cost would require per-asset cost; here we show ending MTM; realized already above
        end_val  = open_q * price

        total_unreal += unreal
        total_end_val += end_val

        line = (
            f"• {_name(asset, addr)}: "
            f"IN {in_qty:.6f} (${in_usd:.6f}) | "
            f"OUT {out_qty:.6f} (${out_usd:.6f}) | "
            f"NET {net_qty:.6f} | REAL ${realized:.6f} | "
            f"OPEN {open_q:.6f} @ ${price:.6f} = ${end_val:.6f}"
        )
        if asset == "CRO":
            cro_line = line
        elif asset in ("USDT","USDC"):
            usdt_line = line
        else:
            lines.append(line)

    # Put CRO & USDT top
    head_lines = []
    if cro_line:
        head_lines.append(cro_line)
    if usdt_line:
        head_lines.append(usdt_line)
    lines = head_lines + lines

    lines.append("")
    lines.append(f"Σύνολο Ending MTM (από merged holdings): ${total_end_val:.6f}")
    lines.append(f"Σύνολο Realized ({scope}): ${sum(float(x['realized_usd']) for x in rows):.6f}")
    return "\n".join(lines)

def format_day_report():
    # uses merged holdings for MTM inside the composer
    return _compose_day_report(
        date_str=ymd(),
        entries=read_json(data_file_for_today(), default={"entries": []}).get("entries", []),
        net_flow=read_json(data_file_for_today(), default={"net_usd_flow": 0.0}).get("net_usd_flow", 0.0),
        realized_today_total=read_json(data_file_for_today(), default={"realized_pnl": 0.0}).get("realized_pnl", 0.0),
        holdings_total=compute_holdings_merged()[0],
        breakdown=compute_holdings_merged()[1],
        unrealized=compute_holdings_merged()[2],
        data_dir=DATA_DIR,
    )

# ------------------------------------------------------------
# Telegram long-poll
# ------------------------------------------------------------
def _handle_update(upd: dict):
    msg = upd.get("message") or upd.get("edited_message") or {}
    text = (msg.get("text") or "").strip()
    if not text:
        return
    cmd, *tail = text.split()
    cmd = cmd.lower()

    if cmd in ("/start", "/help", "/status"):
        send_telegram("✅ Running. Wallet monitor active.\n❓ Commands: /diag /rescan /holdings /show /pnl [today|month|all] /totals [today|month|all] /report /dailysum /crobal")

    elif cmd == "/diag":
        tracked = "(n/a)"
        send_telegram(
            "🔧 Diagnostics\n"
            f"WALLETADDRESS: {WALLET_ADDRESS}\n"
            f"CRONOSRPCURL set: {bool(CRONOS_RPC_URL)}\n"
            f"Etherscan key: {bool(ETHERSCAN_API)}\n"
            f"TZ={TZ}\n"
            f"Tracked pairs: {tracked}"
        )

    elif cmd in ("/rescan",):
        # noop placeholder; real scanning happens continuously, but send a summary
        # Count positive runtime tokens
        pos = sum(1 for v in _token_balances.values() if float(v) > EPSILON)
        send_telegram(f"🔄 Rescan done. Positive tokens: {pos}")

    elif cmd in ("/holdings", "/show", "/show_wallet_assets", "/showwalletassets"):
        send_telegram(format_holdings())

    elif cmd in ("/totals", "/totalstoday", "/totalsmonth"):
        scope = "all"
        if cmd == "/totalstoday":
            scope = "today"
        elif cmd == "/totalsmonth":
            scope = "month"
        else:
            if tail and tail[0].lower() in ("today","month","all"):
                scope = tail[0].lower()
        send_telegram(format_totals(scope))

    elif cmd in ("/pnl",):
        scope = "today"
        if tail and tail[0].lower() in ("today","month","all"):
            scope = tail[0].lower()
        send_telegram(format_pnl(scope))

    elif cmd in ("/report","/showdaily","/dailysum"):
        # Prefer the detailed composer if present; otherwise fallback
        try:
            send_telegram(format_day_report())
        except Exception:
            # fallback mini daily summary constructed from entries
            send_telegram("🧾 Δεν υπάρχουν σημερινές κινήσεις ({})".format(ymd()))

    elif cmd in ("/crobal","/cro","/crodebug"):
        try:
            ok = rpc_init()
            if not ok:
                send_telegram("❌ RPC not connected. Check CRONOS_RPC_URL.")
            else:
                bal = rpc_get_native_balance(WALLET_ADDRESS)
                prc = get_price_usd("CRO") or 0.0
                send_telegram(f"🧪 CRO Native Balance\nBalance: {bal:.6f} CRO\nPrice: ${prc:.6f}\nMTM: ${bal*prc:,.4f}")
        except Exception as ex:
            send_telegram(f"❌ CRO balance debug error: {ex}")

    else:
        send_telegram("❓ Commands: /status /diag /rescan /holdings /show /pnl [today|month|all] /totals [today|month|all] /report /dailysum /crobal")

def telegram_long_poll_loop():
    import requests
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; Telegram updates disabled.")
        return
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    url = f"{base_url}/getUpdates"
    offset_path = os.path.join(DATA_DIR, "telegram_offset.json")

    def _read_offset():
        try:
            return int(read_json(offset_path, {"offset": 0}).get("offset", 0))
        except Exception:
            return 0
    def _write_offset(off: int):
        try:
            write_json(offset_path, {"offset": int(off)})
        except Exception:
            pass

    offset = _read_offset()
    params = {"timeout": 50, "offset": offset}
    try:
        send_telegram("🤖 Telegram command handler online.")
    except Exception:
        pass

    while not shutdown_event.is_set():
        try:
            r = requests.get(url, params=params, timeout=60)
            if r.status_code == 429:
                try:
                    ra = float(r.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    ra = 1.0
                time.sleep(max(1.0, ra))
                continue
            if r.status_code != 200:
                time.sleep(0.8 + random.random())
                continue
            data = r.json()
            if not isinstance(data, dict) or not data.get("ok"):
                time.sleep(0.5 + random.random())
                continue
            result = data.get("result") or []
            if not result:
                continue
            max_update_id = offset
            for upd in result:
                try:
                    upid = int(upd.get("update_id", 0))
                    if upid > max_update_id:
                        max_update_id = upid
                    _handle_update(upd)
                except Exception as ex:
                    log.exception("telegram update dispatch error: %s", ex)
            new_offset = max_update_id + 1
            if new_offset != offset:
                offset = new_offset
                _write_offset(offset)
                params["offset"] = offset
        except requests.Timeout:
            continue
        except Exception as ex:
            log.warning("telegram long-poll error: %s", ex)
            time.sleep(1.0 + random.random())

    log.info("Telegram long-poll loop exited (shutdown).")

# ------------------------------------------------------------
# Scheduler (optional: intraday / EOD hooks)
# ------------------------------------------------------------
def scheduler_loop():
    send_telegram("⏱ Scheduler online (intraday/EOD).")
    last_day = ymd()
    while not shutdown_event.is_set():
        try:
            # EOD fire near 23:59 local
            now = now_dt()
            if ymd(now) != last_day and now.hour >= 0:
                try:
                    send_telegram(format_day_report())
                except Exception:
                    pass
                last_day = ymd(now)
        except Exception as ex:
            log.warning("scheduler error: %s", ex)
        time.sleep(15)

# ------------------------------------------------------------
# Signals & Main
# ------------------------------------------------------------
def _on_sigterm(signum, frame):
    trigger_shutdown("SIGTERM")

def main():
    send_telegram("🟢 Starting Cronos DeFi Sentinel.")
    signal.signal(signal.SIGTERM, _on_sigterm)
    signal.signal(signal.SIGINT,  _on_sigterm)

    # Threads
    threads = []
    threads.append(threading.Thread(target=wallet_monitor_loop, name="wallet", daemon=True))
    threads.append(threading.Thread(target=telegram_long_poll_loop, name="tg", daemon=True))
    threads.append(threading.Thread(target=scheduler_loop, name="sched", daemon=True))

    for t in threads:
        t.start()

    # Guard loop to keep main alive
    try:
        while not shutdown_event.is_set():
            time.sleep(1.0)
    finally:
        send_telegram("🛑 Shutting down.")

if __name__ == "__main__":
    main()
