#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (refactored)
- RPC snapshot (CRO + ERC-20)
- Dexscreener pricing (+history fallback) with canonical CRO via WCRO/USDT
- Cost-basis PnL (Decimal + FIFO, fees optional)
- Intraday/EOD reports
- Alerts & Guard window
- Telegram long-poll with backoff + offset persistence
- Thread-safe shared state with locks
- Reconciled holdings (RPC wins over History; no double counting; receipts excluded from CRO)
"""

import os, sys, time, json, threading, logging, signal, random
from collections import deque, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, getcontext
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# ---------- Precision ----------
getcontext().prec = 28  # high precision for PnL math

# ---------- Helpers & Module Imports ----------
from utils.http import safe_get, safe_json
from telegram.api import send_telegram
from telegram.formatters import escape_md, format_holdings
from reports.day_report import build_day_report_text as _compose_day_report
from reports.ledger import append_ledger, update_cost_basis as ledger_update_cost_basis, replay_cost_basis_over_entries
from reports.aggregates import aggregate_per_asset

from core.config import apply_env_aliases
from core.pricing import get_price_usd, get_change_and_price_for_symbol_or_addr
from core.rpc import rpc_init, get_native_balance as rpc_get_native_balance, erc20_balance as rpc_get_erc20_balance, get_symbol_decimals as rpc_get_symbol_decimals, discover_wallet_tokens
from core.holdings import get_wallet_snapshot
from core.tz import tz_init

# ---------- Bootstrap / TZ ----------
load_dotenv()
apply_env_aliases()

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = tz_init(TZ)
def now_dt():
    return datetime.now(LOCAL_TZ)
def ymd(dt=None):
    return (dt or now_dt()).strftime("%Y-%m-%d")
def month_prefix(dt=None):
    return (dt or now_dt()).strftime("%Y-%m")

# ---------- ENV ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API") or ""
CRONOS_RPC_URL     = os.getenv("CRONOS_RPC_URL") or ""

TOKENS             = os.getenv("TOKENS", "")
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")

LOG_SCAN_BLOCKS = int(os.getenv("LOG_SCAN_BLOCKS", "120000"))
LOG_SCAN_CHUNK  = int(os.getenv("LOG_SCAN_CHUNK",  "5000"))
WALLET_POLL     = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL        = int(os.getenv("DEX_POLL", "60"))
PRICE_WINDOW    = int(os.getenv("PRICE_WINDOW", "3"))
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5"))
SPIKE_THRESHOLD      = float(os.getenv("SPIKE_THRESHOLD", "8"))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

DISCOVER_ENABLED  = (os.getenv("DISCOVER_ENABLED", "true").lower() in ("1", "true", "yes", "on"))
DISCOVER_QUERY    = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT    = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL     = int(os.getenv("DISCOVER_POLL", "120"))
DISCOVER_MIN_LIQ_USD        = float(os.getenv("DISCOVER_MIN_LIQ_USD", "30000"))
DISCOVER_MIN_VOL24_USD      = float(os.getenv("DISCOVER_MIN_VOL24_USD", "5000"))
DISCOVER_MIN_ABS_CHANGE_PCT = float(os.getenv("DISCOVER_MIN_ABS_CHANGE_PCT", "10"))
DISCOVER_MAX_PAIR_AGE_HOURS = int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS", "24"))
DISCOVER_REQUIRE_WCRO       = (os.getenv("DISCOVER_REQUIRE_WCRO", "false").lower() in ("1", "true", "yes", "on"))
DISCOVER_BASE_WHITELIST     = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_WHITELIST", "").split(",") if s.strip()]
DISCOVER_BASE_BLACKLIST     = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_BLACKLIST", "").split(",") if s.strip()]

INTRADAY_HOURS  = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR        = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE      = int(os.getenv("EOD_MINUTE", "59"))

ALERTS_INTERVAL_MIN = int(os.getenv("ALERTS_INTERVAL_MIN", "15"))
DUMP_ALERT_24H_PCT  = float(os.getenv("DUMP_ALERT_24H_PCT", "-15"))
PUMP_ALERT_24H_PCT  = float(os.getenv("PUMP_ALERT_24H_PCT", "20"))

GUARD_WINDOW_MIN     = int(os.getenv("GUARD_WINDOW_MIN", "60"))
GUARD_PUMP_PCT       = float(os.getenv("GUARD_PUMP_PCT", "20"))
GUARD_DROP_PCT       = float(os.getenv("GUARD_DROP_PCT", "-12"))
GUARD_TRAIL_DROP_PCT = float(os.getenv("GUARD_TRAIL_DROP_PCT", "-8"))

RECEIPT_SYMBOLS = set([s.strip().upper() for s in os.getenv("RECEIPT_SYMBOLS", "TCRO").split(",") if s.strip()])

# ---------- Constants / Logging ----------
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"
DATA_DIR         = "/app/data"
ATH_PATH         = os.path.join(DATA_DIR, "ath.json")
OFFSET_PATH      = os.path.join(DATA_DIR, "telegram_offset.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("wallet-monitor")

# ---------- Locks & Runtime ----------
BAL_LOCK = threading.RLock()
POS_LOCK = threading.RLock()
GUARD_LOCK = threading.RLock()
SEEN_LOCK = threading.RLock()

shutdown_event = threading.Event()
_seen_tx_hashes = set()
_last_prices, _price_history, _last_pair_tx = {}, {}, {}
_tracked_pairs, _known_pairs_meta = set(), {}

_TOKEN_EVENT_LRU_MAX, _TOKEN_HASH_LRU_MAX = 4000, 2000
_seen_token_events, _seen_token_hashes = set(), set()
_seen_token_events_q, _seen_token_hashes_q = deque(maxlen=_TOKEN_EVENT_LRU_MAX), deque(maxlen=_TOKEN_HASH_LRU_MAX)

_token_balances = defaultdict(Decimal)
_token_meta     = {}

_position_qty   = defaultdict(Decimal)
_position_cost  = defaultdict(Decimal)
_realized_pnl_today = Decimal("0")

EPSILON = Decimal("1e-12")
_last_intraday_sent = 0.0

ATH, _alert_last_sent = {}, {}
COOLDOWN_SEC = 60 * 30
_guard = {}  # key -> {"entry", "peak", "start_ts"}

os.makedirs(DATA_DIR, exist_ok=True)

# ---------- Utils ----------
def _format_amount(a):
    try:
        a = float(a)
    except:
        return str(a)
    if abs(a) >= 1:
        return f"{a:,.4f}"
    if abs(a) >= 0.0001:
        return f"{a:.6f}"
    return f"{a:.8f}"

def _format_price(p):
    try:
        p = float(p)
    except:
        return str(p)
    if p >= 1:
        return f"{p:,.6f}"
    if p >= 0.01:
        return f"{p:.6f}"
    if p >= 1e-6:
        return f"{p:.8f}"
    return f"{p:.10f}"

def _nonzero(v, eps=Decimal("1e-12")):
    try:
        return abs(Decimal(str(v))) > eps
    except:
        return False

def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return default

def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

# ---------- ATH ----------
def load_ath():
    global ATH
    ATH = read_json(ATH_PATH, default={})
    if not isinstance(ATH, dict):
        ATH = {}

def save_ath():
    write_json(ATH_PATH, ATH)

def update_ath(key: str, live_price: float):
    if not _nonzero(live_price):
        return
    prev = ATH.get(key)
    if prev is None or live_price > prev + 1e-12:
        ATH[key] = live_price
        save_ath()
        send_telegram(f"🏆 New ATH {escape_md(key)}: ${_format_price(live_price)}")

# ---------- Pricing & History ----------
# (Uses core.pricing via get_price_usd and get_change_and_price_for_symbol_or_addr; history prices populated during data load)

# ---------- Etherscan ----------
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
        "apikey": ETHERSCAN_API
    }
    data = safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=2)) or {}
    if str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
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
        "apikey": ETHERSCAN_API
    }
    data = safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=2)) or {}
    if str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

# ---------- Cost-basis replay (today) ----------
def _replay_today_cost_basis():
    global _position_qty, _position_cost, _realized_pnl_today
    with POS_LOCK:
        _position_qty.clear()
        _position_cost.clear()
        _realized_pnl_today = Decimal("0")
        path = data_file_for_today()
        data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        total_realized = replay_cost_basis_over_entries(_position_qty, _position_cost, data.get("entries", []), eps=float(EPSILON))
        _realized_pnl_today = Decimal(str(total_realized))
        data["realized_pnl"] = float(total_realized)
        write_json(path, data)

# ---------- History maps ----------
def _build_history_maps():
    """
    Build mapping from token symbols to contract addresses from all history files and update price cache.
    """
    symbol_to_contract = {}
    symbol_conflict = set()
    files = []
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"):
                files.append(fn)
    except Exception as ex:
        log.exception("listdir data error: %s", ex)
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
                try:
                    import core.pricing
                    if addr and addr.startswith("0x"):
                        core.pricing.HISTORY_LAST_PRICE[addr] = p
                    if sym:
                        core.pricing.HISTORY_LAST_PRICE[sym.upper()] = p
                except Exception:
                    pass
            if sym and addr and addr.startswith("0x"):
                if sym in symbol_to_contract and symbol_to_contract[sym] != addr:
                    symbol_conflict.add(sym)
                else:
                    symbol_to_contract.setdefault(sym, addr)
    for s in symbol_conflict:
        symbol_to_contract.pop(s, None)
    return symbol_to_contract

# ---------- RPC & Holdings ----------
def _rpc_balances_snapshot():
    """
    Return list of current holdings (CRO + ERC20) using live RPC data.
    Each entry: {"key": ..., "token": ..., "addr": ..., "amount": Decimal, "price": float}
    """
    breakdown = []
    # Populate historical price cache
    _ = _build_history_maps()
    # Native CRO
    cro_amt = 0.0
    if rpc_init():
        try:
            cro_amt = rpc_get_native_balance(WALLET_ADDRESS)
        except Exception:
            cro_amt = 0.0
    if cro_amt > float(EPSILON):
        cro_price = get_price_usd("CRO") or 0.0
        breakdown.append({
            "key": "CRO",
            "token": "CRO",
            "addr": None,
            "amount": Decimal(str(cro_amt)),
            "price": float(cro_price)
        })
    # ERC-20 tokens
    tokens_list = []
    try:
        tokens_list = discover_wallet_tokens(LOG_SCAN_BLOCKS, LOG_SCAN_CHUNK) or []
    except Exception as e:
        log.warning("discover_wallet_tokens failed: %s", e)
        tokens_list = []
    # Include any env-specified tokens that might have been missed
    try:
        token_items = [t.strip().lower() for t in TOKENS.split(",") if t.strip()]
        found_addrs = {t.get("token_addr") for t in tokens_list}
        for t in token_items:
            addr = ""
            if t.startswith("cronos/"):
                _, addr = t.split("/", 1)
            elif t.startswith("0x") and len(t) == 42:
                addr = t
            if addr and addr not in found_addrs:
                try:
                    bal = rpc_get_erc20_balance(addr, WALLET_ADDRESS)
                except Exception:
                    bal = 0.0
                if bal and bal > float(EPSILON):
                    try:
                        sym, dec = rpc_get_symbol_decimals(addr)
                    except Exception:
                        sym, dec = addr[:8].upper(), 18
                    tokens_list.append({
                        "token_addr": addr.lower(),
                        "symbol": sym,
                        "decimals": dec,
                        "balance": bal
                    })
                    found_addrs.add(addr)
    except Exception as e:
        log.debug("Env token check error: %s", e)
    for token in sorted(tokens_list, key=lambda x: float(x.get("balance", 0)), reverse=True):
        addr = token.get("token_addr") or ""
        bal = float(token.get("balance") or 0)
        if bal <= float(EPSILON):
            continue
        sym = token.get("symbol") or (addr[:8].upper() if addr else "?")
        pr = get_price_usd(addr if addr else sym) or 0.0
        breakdown.append({
            "key": addr if addr else sym,
            "token": sym,
            "addr": addr if addr else None,
            "amount": Decimal(str(bal)),
            "price": float(pr)
        })
    return breakdown

def _history_positions_snapshot():
    """
    Return list of holdings (open positions) derived from history (excluding closed positions).
    """
    pos_qty, pos_cost = rebuild_open_positions_from_history()
    out = []
    for key, amt in pos_qty.items():
        amt = Decimal(str(amt))
        if amt <= EPSILON:
            continue
        sym = (_token_meta.get(key, {}).get("symbol") if isinstance(key, str) and key.startswith("0x") else str(key)) or "?"
        price = None
        if isinstance(key, str) and key.startswith("0x"):
            price = get_price_usd(key)
        else:
            price = get_price_usd(sym)
        if price is None or price <= 0:
            try:
                import core.pricing
                hist_price = core.pricing.HISTORY_LAST_PRICE.get(key.upper()) or core.pricing.HISTORY_LAST_PRICE.get(sym.upper())
            except Exception:
                hist_price = None
            price = float(hist_price or 0.0)
        out.append({
            "key": key if (isinstance(key, str) and key.startswith("0x")) else sym,
            "token": sym,
            "addr": key if (isinstance(key, str) and key.startswith("0x")) else None,
            "amount": amt,
            "price": float(price or 0.0)
        })
    return out

def rebuild_open_positions_from_history():
    pos_qty, pos_cost = defaultdict(Decimal), defaultdict(Decimal)
    symbol_to_contract = _build_history_maps()
    def _update(pos_qty, pos_cost, token_key, signed_amount, price_usd):
        qty = pos_qty[token_key]
        cost = pos_cost[token_key]
        amt = Decimal(str(signed_amount))
        price = Decimal(str(price_usd or 0))
        if amt > EPSILON:
            pos_qty[token_key] = qty + amt
            pos_cost[token_key] = cost + amt * price
        elif amt < -EPSILON and qty > EPSILON:
            sell_qty = min(-amt, qty)
            avg_cost = (cost / qty) if qty > EPSILON else price
            pos_qty[token_key] = qty - sell_qty
            pos_cost[token_key] = max(Decimal("0"), cost - avg_cost * sell_qty)
    files = []
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"):
                files.append(fn)
    except Exception as ex:
        log.exception("listdir data error: %s", ex)
    files.sort()
    for fn in files:
        data = read_json(os.path.join(DATA_DIR, fn), default=None)
        if not isinstance(data, dict):
            continue
        for e in data.get("entries", []):
            sym_raw = (e.get("token") or "").strip()
            addr_raw = (e.get("token_addr") or "").strip().lower()
            amt = Decimal(str(e.get("amount") or 0.0))
            pr = Decimal(str(e.get("price_usd") or 0.0))
            symU = sym_raw.upper() if sym_raw else sym_raw
            if addr_raw and addr_raw.startswith("0x"):
                key = addr_raw
            else:
                mapped = symbol_to_contract.get(sym_raw) or symbol_to_contract.get(symU)
                key = mapped if (mapped and mapped.startswith("0x")) else (symU or sym_raw or "?")
            _update(pos_qty, pos_cost, key, amt, pr)
    for k, v in list(pos_qty.items()):
        if abs(v) < Decimal("1e-10"):
            pos_qty[k] = Decimal("0")
    return pos_qty, pos_cost

def compute_holdings_merged():
    """
    Merge RPC + History holdings without double counting:
    - RPC balances take precedence.
    - History positions included for tokens not in RPC (e.g., open positions with zero wallet balance).
    Returns: total_usd, breakdown(list), unrealized_usd, receipts(list)
    """
    rpc = {rec["key"]: rec for rec in _rpc_balances_snapshot()}
    hist = {rec["key"]: rec for rec in _history_positions_snapshot()}
    merged = {}
    receipts = []
    def _is_receipt(sym: str) -> bool:
        return (sym or "").upper() in RECEIPT_SYMBOLS
    keys = set(rpc.keys()) | set(hist.keys())
    for k in keys:
        r = rpc.get(k)
        h = hist.get(k)
        rec = r or h
        if not rec:
            continue
        symU = (rec.get("token") or "").upper()
        amt = Decimal(str((r or h)["amount"]))
        price = 0.0
        if r and float(r.get("price") or 0) > 0:
            price = float(r["price"])
        elif h:
            price = float(h.get("price") or 0.0)
        out = {
            "token": rec.get("token"),
            "token_addr": rec.get("addr"),
            "amount": float(amt),
            "price_usd": float(price),
            "usd_value": float(amt) * float(price)
        }
        if _is_receipt(symU) or symU == "TCRO":
            receipts.append(out)
        else:
            merged[k] = out
        if r and h:
            try:
                diff = abs(float(r["amount"]) - float(h["amount"]))
                if diff > 1e-9:
                    log.warning("merge divergence %s: rpc=%s history=%s", k, r["amount"], h["amount"])
            except:
                pass
    breakdown = sorted(merged.values(), key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    unrealized = 0.0
    with POS_LOCK:
        for b in breakdown:
            key = b["token_addr"] if b.get("token_addr") else str(b["token"]).upper()
            qty = float(_position_qty.get(key, Decimal("0")))
            cost = float(_position_cost.get(key, Decimal("0")))
            pr = float(b.get("price_usd") or 0.0)
            if qty > 0 and pr > 0:
                unrealized += qty * pr - cost
    total = sum(float(b["usd_value"]) for b in breakdown)
    return total, breakdown, unrealized, receipts

# ---------- Day report wrapper ----------
def build_day_report_text():
    date_str = ymd()
    path = data_file_for_today()
    data = read_json(path, default={"date": date_str, "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])
    net_flow = float(data.get("net_usd_flow", 0.0))
    realized_today_total = float(data.get("realized_pnl", 0.0))
    holdings_total, breakdown, unrealized, _receipts = compute_holdings_merged()
    return _compose_day_report(
        date_str=date_str, entries=entries, net_flow=net_flow,
        realized_today_total=realized_today_total, holdings_total=holdings_total,
        breakdown=breakdown, unrealized=unrealized, data_dir=DATA_DIR, tz=LOCAL_TZ
    )

# ---------- Mini summaries & TX handlers ----------
def _mini_summary_line(token_key, symbol_shown):
    with POS_LOCK:
        open_qty = _position_qty.get(token_key, Decimal("0"))
        open_cost = _position_cost.get(token_key, Decimal("0"))
    live = None
    if token_key == "CRO":
        live = get_price_usd("CRO") or 0.0
    elif isinstance(token_key, str) and token_key.startswith("0x"):
        live = get_price_usd(token_key) or 0.0
    else:
        live = get_price_usd(symbol_shown) or 0.0
    unreal = 0.0
    if open_qty > EPSILON and _nonzero(live):
        unreal = float(open_qty) * float(live) - float(open_cost)
    send_telegram(
        f"• {'Open' if open_qty > 0 else 'Flat'} {escape_md(symbol_shown)} {_format_amount(open_qty)} @ live ${_format_price(live)}\n"
        f"   Avg: ${_format_price((float(open_cost)/float(open_qty)) if open_qty > EPSILON else 0)} | Unreal: ${_format_amount(unreal)}"
    )

def _remember_token_event(key_tuple):
    with SEEN_LOCK:
        if key_tuple in _seen_token_events:
            return False
        _seen_token_events.add(key_tuple)
        _seen_token_events_q.append(key_tuple)
        if len(_seen_token_events_q) == _TOKEN_EVENT_LRU_MAX:
            while len(_seen_token_events) > _TOKEN_EVENT_LRU_MAX:
                old = _seen_token_events_q.popleft()
                if old in _seen_token_events:
                    _seen_token_events.remove(old)
    return True

def _remember_token_hash(h):
    if not h:
        return True
    with SEEN_LOCK:
        if h in _seen_token_hashes:
            return False
        _seen_token_hashes.add(h)
        _seen_token_hashes_q.append(h)
        if len(_seen_token_hashes_q) == _TOKEN_HASH_LRU_MAX:
            while len(_seen_token_hashes) > _TOKEN_HASH_LRU_MAX:
                old = _seen_token_hashes_q.popleft()
                if old in _seen_token_hashes:
                    _seen_token_hashes.remove(old)
    return True

def handle_native_tx(tx: dict):
    h = tx.get("hash")
    with SEEN_LOCK:
        if not h or h in _seen_tx_hashes:
            return
        _seen_tx_hashes.add(h)
    val_raw = tx.get("value", "0")
    try:
        amount_cro = int(val_raw) / (10**18)
    except:
        try:
            amount_cro = float(val_raw)
        except:
            amount_cro = 0.0
    frm = (tx.get("from") or "").lower()
    to = (tx.get("to") or "").lower()
    ts = int(tx.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(ts, LOCAL_TZ) if ts > 0 else now_dt()
    sign = +1 if to == WALLET_ADDRESS else (-1 if frm == WALLET_ADDRESS else 0)
    if sign == 0 or abs(amount_cro) <= float(EPSILON):
        return
    price = get_price_usd("CRO") or 0.0
    usd_value = sign * amount_cro * (price or 0.0)
    with BAL_LOCK:
        _token_balances["CRO"] += Decimal(str(sign * amount_cro))
        _token_meta["CRO"] = {"symbol": "CRO", "decimals": 18}
    with POS_LOCK:
        realized = ledger_update_cost_basis(_position_qty, _position_cost, "CRO", float(sign * amount_cro), float(price), eps=float(EPSILON))
    link = CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Native TX* ({'IN' if sign > 0 else 'OUT'}) CRO\n"
        f"Hash: {escape_md(link)}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign * amount_cro:.6f} CRO\nPrice: ${_format_price(price)}\nUSD value: ${_format_amount(usd_value)}"
    )
    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h, "type": "native",
        "token": "CRO", "token_addr": None,
        "amount": sign * amount_cro,
        "price_usd": price, "usd_value": usd_value,
        "realized_pnl": float(realized),
        "from": frm, "to": to
    })

def handle_erc20_tx(t: dict):
    h = t.get("hash") or ""
    frm = (t.get("from") or "").lower()
    to = (t.get("to") or "").lower()
    token_addr = (t.get("contractAddress") or "").lower()
    symbol = t.get("tokenSymbol") or (token_addr[:8] if token_addr else "?")
    try:
        decimals = int(t.get("tokenDecimal") or 18)
    except:
        decimals = 18
    val_raw = t.get("value", "0")
    event_key = (h, token_addr, frm, to, str(val_raw), str(decimals))
    if not _remember_token_event(event_key):
        return
    _remember_token_hash(h)
    if WALLET_ADDRESS not in (frm, to):
        return
    try:
        amount = int(val_raw) / (10 ** decimals)
    except:
        try:
            amount = float(val_raw)
        except:
            amount = 0.0
    ts = int(t.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(ts, LOCAL_TZ) if ts > 0 else now_dt()
    sign = +1 if to == WALLET_ADDRESS else -1
    price = None
    if token_addr and token_addr.startswith("0x"):
        price = get_price_usd(token_addr) or 0.0
    else:
        price = get_price_usd(symbol) or 0.0
    usd_value = sign * amount * (price or 0.0)
    key = token_addr if token_addr else symbol
    with BAL_LOCK:
        _token_balances[key] += Decimal(str(sign * amount))
        if abs(_token_balances[key]) < Decimal("1e-10"):
            _token_balances[key] = Decimal("0")
        _token_meta[key] = {"symbol": symbol, "decimals": decimals}
    with POS_LOCK:
        realized = ledger_update_cost_basis(_position_qty, _position_cost, key, float(sign * amount), float(price or 0.0), eps=float(EPSILON))
    try:
        if _nonzero(price):
            update_ath(token_addr if token_addr else symbol, price)
    except:
        pass
    link = CRONOS_TX.format(txhash=h)
    direction = "IN" if sign > 0 else "OUT"
    send_telegram(
        f"Token TX ({direction}) {escape_md(symbol)}\nHash: {escape_md(link)}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign * amount:.6f} {escape_md(symbol)}\nPrice: ${_format_price(price)}\nUSD value: ${_format_amount(usd_value)}"
    )
    send_telegram(f"• {'BUY' if sign > 0 else 'SELL'} {escape_md(symbol)} {_format_amount(abs(amount))} @ live ${_format_price(price)}")
    _mini_summary_line(key, symbol)
    if sign > 0 and _nonzero(price):
        with GUARD_LOCK:
            _guard[key] = {"entry": float(price), "peak": float(price), "start_ts": time.time()}
    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h or None,
        "type": "erc20",
        "token": symbol, "token_addr": token_addr or None,
        "amount": sign * amount, "price_usd": price or 0.0, "usd_value": usd_value,
        "realized_pnl": float(realized),
        "from": frm, "to": to
    })

# ---------- Dex monitor & discovery ----------
def slug(chain: str, pair_address: str) -> str:
    return f"{chain}/{pair_address}".lower()

def fetch_pair(slg_str: str):
    return safe_json(safe_get(f"{DEX_BASE_PAIRS}/{slg_str}", timeout=12, retries=2))

def fetch_token_pairs(chain: str, token_address: str):
    data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/{chain}/{token_address}", timeout=12, retries=2)) or {}
    return data.get("pairs") or []

def fetch_search(query: str):
    data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=15, retries=2)) or {}
    return data.get("pairs") or []

def ensure_tracking_pair(chain: str, pair_address: str, meta: dict = None):
    s = slug(chain, pair_address)
    if s in _tracked_pairs:
        return
    _tracked_pairs.add(s)
    _last_prices[s] = None
    _last_pair_tx[s] = None
    _price_history[s] = deque(maxlen=PRICE_WINDOW)
    if meta:
        _known_pairs_meta[s] = meta
    ds_link = f"https://dexscreener.com/{chain}/{pair_address}"
    sym = None
    if isinstance(meta, dict):
        bt = meta.get("baseToken") or {}
        sym = bt.get("symbol")
    title = f"{sym} ({s})" if sym else s
    send_telegram(f"🆕 Now monitoring pair: {escape_md(title)}\n{escape_md(ds_link)}")

def update_price_history(slg, price):
    hist = _price_history.get(slg) or deque(maxlen=PRICE_WINDOW)
    _price_history[slg] = hist
    hist.append(price)
    _last_prices[slg] = price

def detect_spike(slg):
    hist = _price_history.get(slg)
    if not hist or len(hist) < 2:
        return None
    first, last = hist[0], hist[-1]
    if not first:
        return None
    pct = (last - first) / first * 100.0
    return pct if abs(pct) >= SPIKE_THRESHOLD else None

_last_pair_alert = {}
PAIR_ALERT_COOLDOWN = 60 * 10

def _pair_cooldown_ok(key):
    last = _last_pair_alert.get(key, 0.0)
    now = time.time()
    if now - last >= PAIR_ALERT_COOLDOWN:
        _last_pair_alert[key] = now
        return True
    return False

def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        log.info("No tracked pairs; monitor waits.")
    else:
        send_telegram(f"🚀 Dex monitor started: {escape_md(', '.join(sorted(_tracked_pairs)))}")
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
                if isinstance(data.get("pair"), dict):
                    pair = data["pair"]
                elif isinstance(data.get("pairs"), list) and data["pairs"]:
                    pair = data["pairs"][0]
                if not pair:
                    continue
                price_val = None
                try:
                    price_val = float(pair.get("priceUsd") or 0)
                except:
                    price_val = None
                if price_val and price_val > 0:
                    update_price_history(s, price_val)
                    spike_pct = detect_spike(s)
                    if spike_pct is not None:
                        try:
                            vol_h1 = float((pair.get("volume") or {}).get("h1") or 0)
                        except:
                            vol_h1 = None
                        if not (MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 < MIN_VOLUME_FOR_ALERT):
                            bt = pair.get("baseToken") or {}
                            symbol = bt.get("symbol") or s
                            if _pair_cooldown_ok(f"spike:{s}"):
                                send_telegram(f"🚨 Spike on {escape_md(symbol)}: {spike_pct:.2f}%\nPrice: ${_format_price(price_val)}")
                                _price_history[s].clear()
                                _last_prices[s] = price_val
                prev = _last_prices.get(s)
                if prev and price_val and prev > 0:
                    delta = (price_val - prev) / prev * 100.0
                    if abs(delta) >= PRICE_MOVE_THRESHOLD and _pair_cooldown_ok(f"move:{s}"):
                        bt = pair.get("baseToken") or {}
                        symbol = bt.get("symbol") or s
                        send_telegram(f"📈 Price move on {escape_md(symbol)}: {delta:.2f}%\nPrice: ${_format_price(price_val)} (prev ${_format_price(prev)})")
                        _last_prices[s] = price_val
                last_tx = (pair.get("lastTx") or {}).get("hash")
                if last_tx:
                    prev_tx = _last_pair_tx.get(s)
                    if prev_tx != last_tx and _pair_cooldown_ok(f"trade:{s}"):
                        _last_pair_tx[s] = last_tx
                        bt = pair.get("baseToken") or {}
                        symbol = bt.get("symbol") or s
                        send_telegram(f"🔔 New trade on {escape_md(symbol)}\nTx: {escape_md(CRONOS_TX.format(txhash=last_tx))}")
            except Exception as e:
                log.debug("pairs loop error %s: %s", s, e)
        sleep_s = DEX_POLL + random.uniform(-2, 2)
        for _ in range(int(max(1, sleep_s))):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def _pair_passes_filters(p):
    try:
        if str(p.get("chainId", "")).lower() != "cronos":
            return False
        bt = p.get("baseToken") or {}
        qt = p.get("quoteToken") or {}
        base_sym = (bt.get("symbol") or "").upper()
        quote_sym = (qt.get("symbol") or "").upper()
        if DISCOVER_REQUIRE_WCRO and quote_sym != "WCRO":
            return False
        if DISCOVER_BASE_WHITELIST and base_sym not in DISCOVER_BASE_WHITELIST:
            return False
        if DISCOVER_BASE_BLACKLIST and base_sym in DISCOVER_BASE_BLACKLIST:
            return False
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq < DISCOVER_MIN_LIQ_USD:
            return False
        vol24 = float((p.get("volume") or {}).get("h24") or 0)
        if vol24 < DISCOVER_MIN_VOL24_USD:
            return False
        ch = p.get("priceChange") or {}
        best_change = 0.0
        for k in ("h1", "h4", "h6", "h24"):
            if k in ch:
                try:
                    best_change = max(best_change, abs(float(ch[k])))
                except:
                    pass
        if best_change < DISCOVER_MIN_ABS_CHANGE_PCT:
            return False
        created_ms = p.get("pairCreatedAt")
        if created_ms:
            age_h = (time.time() * 1000 - float(created_ms)) / 1000 / 3600.0
            if age_h > DISCOVER_MAX_PAIR_AGE_HOURS:
                return False
        return True
    except:
        return False

def discovery_loop():
    seeds = [p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    for s in seeds:
        if s.startswith("cronos/"):
            ensure_tracking_pair("cronos", s.split("/", 1)[1])
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
            found = fetch_search(DISCOVER_QUERY) or []
            adopted = 0
            for p in found:
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
        backoff = DISCOVER_POLL + random.uniform(-3, 3)
        for _ in range(int(max(1, backoff))):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ---------- Alerts & Guard ----------
def _cooldown_ok(key):
    last = _alert_last_sent.get(key, 0.0)
    if time.time() - last >= COOLDOWN_SEC:
        _alert_last_sent[key] = time.time()
        return True
    return False

def get_wallet_balances_snapshot():
    balances = {}
    with BAL_LOCK:
        for k, v in list(_token_balances.items()):
            amt = float(v)
            if amt <= float(EPSILON):
                continue
            if k == "CRO":
                sym = "CRO"
            elif isinstance(k, str) and k.startswith("0x"):
                meta = _token_meta.get(k, {})
                sym = (meta.get("symbol") or k[:8]).upper()
            else:
                sym = str(k).upper()
            balances[sym] = balances.get(sym, 0.0) + amt
    return balances

def alerts_monitor_loop():
    send_telegram(f"🛰 Alerts monitor every {ALERTS_INTERVAL_MIN}m. Wallet 24h dump/pump: {DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT}.")
    while not shutdown_event.is_set():
        try:
            wallet_bal = get_wallet_balances_snapshot()
            for sym, amt in list(wallet_bal.items()):
                if amt <= float(EPSILON):
                    continue
                price, ch24, ch2h, url = get_change_and_price_for_symbol_or_addr(sym)
                if not price or price <= 0:
                    continue
                if ch24 is not None:
                    if ch24 >= PUMP_ALERT_24H_PCT and _cooldown_ok(f"24h_pump:{sym}"):
                        send_telegram(f"🚀 Pump Alert {escape_md(sym)} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{escape_md(url or '')}")
                    if ch24 <= DUMP_ALERT_24H_PCT and _cooldown_ok(f"24h_dump:{sym}"):
                        send_telegram(f"⚠️ Dump Alert {escape_md(sym)} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{escape_md(url or '')}")
            data = read_json(data_file_for_today(), default={"entries": []})
            seen = set()
            for e in data.get("entries", []):
                if float(e.get("amount") or 0) > 0:
                    sym = (e.get("token") or "?").upper()
                    addr = (e.get("token_addr") or "").lower()
                    key = addr if (addr and addr.startswith("0x")) else sym
                    if key in seen:
                        continue
                    seen.add(key)
                    query = addr if (addr and addr.startswith("0x")) else sym
                    price, ch24, ch2h, url = get_change_and_price_for_symbol_or_addr(query)
                    if not price or price <= 0:
                        continue
                    ch = ch2h if (ch2h is not None) else ch24
                    if ch is None:
                        continue
                    if ch >= PUMP_ALERT_24H_PCT and _cooldown_ok(f"risky:pump:{key}"):
                        send_telegram(f"🚀 Pump (recent) {escape_md(sym)} {ch:.2f}%\nPrice ${_format_price(price)}\n{escape_md(url or '')}")
                    if ch <= DUMP_ALERT_24H_PCT and _cooldown_ok(f"risky:dump:{key}"):
                        send_telegram(f"⚠️ Dump (recent) {escape_md(sym)} {ch:.2f}%\nPrice ${_format_price(price)}\n{escape_md(url or '')}")
        except Exception as e:
            log.exception("alerts monitor error: %s", e)
        for _ in range(ALERTS_INTERVAL_MIN * 60):
            if shutdown_event.is_set():
                break
            time.sleep(1)

def guard_monitor_loop():
    send_telegram(f"🛡 Guard monitor: {GUARD_WINDOW_MIN}m window, +{GUARD_PUMP_PCT}% / {GUARD_DROP_PCT}% / trailing {GUARD_TRAIL_DROP_PCT}%.")
    while not shutdown_event.is_set():
        try:
            dead = []
            with GUARD_LOCK:
                items = list(_guard.items())
            for key, st in items:
                if time.time() - st["start_ts"] > GUARD_WINDOW_MIN * 60:
                    dead.append(key)
                    continue
                if key == "CRO":
                    price = get_price_usd("CRO") or 0.0
                elif isinstance(key, str) and key.startswith("0x"):
                    price = get_price_usd(key) or 0.0
                else:
                    meta = _token_meta.get(key, {})
                    sym = meta.get("symbol") or key
                    price = get_price_usd(sym) or 0.0
                if not price or price <= 0:
                    continue
                entry = st.get("entry", 0.0)
                peak = st.get("peak", 0.0)
                if entry <= 0:
                    with GUARD_LOCK:
                        _guard[key]["entry"] = price
                        _guard[key]["peak"] = price
                    entry = price
                    peak = price
                if price > peak:
                    with GUARD_LOCK:
                        _guard[key]["peak"] = price
                    peak = price
                pct_from_entry = (price - entry) / entry * 100.0 if entry > 0 else 0.0
                trail_from_peak = (price - peak) / peak * 100.0 if peak > 0 else 0.0
                sym = _token_meta.get(key, {}).get("symbol") or ("CRO" if key == "CRO" else (key[:6] if isinstance(key, str) else "ASSET"))
                if pct_from_entry >= GUARD_PUMP_PCT and _cooldown_ok(f"guard:pump:{key}"):
                    send_telegram(f"🟢 GUARD Pump {escape_md(sym)} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if pct_from_entry <= GUARD_DROP_PCT and _cooldown_ok(f"guard:drop:{key}"):
                    send_telegram(f"🔻 GUARD Drop {escape_md(sym)} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if trail_from_peak <= GUARD_TRAIL_DROP_PCT and _cooldown_ok(f"guard:trail:{key}"):
                    send_telegram(f"🟠 GUARD Trail {escape_md(sym)} {trail_from_peak:.2f}% from peak ${_format_price(peak)} → ${_format_price(price)}")
            if dead:
                with GUARD_LOCK:
                    for k in dead:
                        _guard.pop(k, None)
        except Exception as e:
            log.exception("guard monitor error: %s", e)
        for _ in range(15):
            if shutdown_event.is_set():
                break
            time.sleep(2)

# ---------- Today per-asset summary ----------
def summarize_today_per_asset():
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": []})
    entries = data.get("entries", [])
    agg = {}
    for e in entries:
        sym = (e.get("token") or "?").upper()
        addr = (e.get("token_addr") or "").lower()
        key = addr if addr.startswith("0x") else sym
        rec = agg.get(key)
        if not rec:
            rec = {
                "symbol": sym,
                "token_addr": addr if addr else None,
                "buy_qty": 0.0,
                "sell_qty": 0.0,
                "net_qty_today": 0.0,
                "net_flow_today": 0.0,
                "realized_today": 0.0,
                "txs": [],
                "last_price_seen": 0.0
            }
            agg[key] = rec
        amt = float(e.get("amount") or 0.0)
        usd = float(e.get("usd_value") or 0.0)
        prc = float(e.get("price_usd") or 0.0)
        rp = float(e.get("realized_pnl") or 0.0)
        tm = (e.get("time", "")[-8:]) or ""
        direction = "IN" if amt > 0 else "OUT"
        rec["txs"].append({"time": tm, "dir": direction, "amount": amt, "price": prc, "usd": usd, "realized": rp})
        if amt > 0:
            rec["buy_qty"] += amt
        if amt < 0:
            rec["sell_qty"] += -amt
        rec["net_qty_today"] += amt
        rec["net_flow_today"] += usd
        rec["realized_today"] += rp
        if prc > 0:
            rec["last_price_seen"] = prc
    result = []
    for key, rec in agg.items():
        if rec["token_addr"]:
            price_now = get_price_usd(rec["token_addr"]) or rec["last_price_seen"]
            gkey = rec["token_addr"]
        else:
            price_now = get_price_usd(rec["symbol"]) or rec["last_price_seen"]
            gkey = rec["symbol"]
        with POS_LOCK:
            open_qty_now = _position_qty.get(gkey, Decimal("0"))
            open_cost_now = _position_cost.get(gkey, Decimal("0"))
        unreal_now = 0.0
        if open_qty_now > EPSILON and _nonzero(price_now):
            unreal_now = float(open_qty_now) * float(price_now) - float(open_cost_now)
        rec["price_now"] = price_now or 0.0
        rec["unreal_now"] = unreal_now
        result.append(rec)
    result.sort(key=lambda r: abs(r["net_flow_today"]), reverse=True)
    return result

def _format_daily_sum_message():
    per = summarize_today_per_asset()
    if not per:
        return f"🧾 Δεν υπάρχουν σημερινές κινήσεις ({ymd()})."
    tot_real = sum(float(r.get("realized_today", 0.0)) for r in per)
    tot_flow = sum(float(r.get("net_flow_today", 0.0)) for r in per)
    tot_unrl = sum(float(r.get("unreal_now", 0.0) or 0.0) for r in per)
    per_sorted = sorted(per, key=lambda r: (abs(float(r.get("realized_today", 0.0))), abs(float(r.get("net_flow_today", 0.0)))), reverse=True)
    lines = [f"*🧾 Daily PnL (Today {ymd()}):*"]
    for r in per_sorted:
        tok = r.get("symbol") or "?"
        flow = float(r.get("net_flow_today", 0.0))
        real = float(r.get("realized_today", 0.0))
        qty = float(r.get("net_qty_today", 0.0))
        pr = float(r.get("price_now", 0.0) or 0.0)
        un = float(r.get("unreal_now", 0.0) or 0.0)
        base = f"• {escape_md(tok)}: realized ${_format_amount(real)} | flow ${_format_amount(flow)} | qty {_format_amount(qty)}"
        if _nonzero(pr):
            base += f" | price ${_format_price(pr)}"
        if _nonzero(un):
            base += f" | unreal ${_format_amount(un)}"
        lines.append(base)
    lines.append("")
    lines.append(f"*Σύνολο realized σήμερα:* ${_format_amount(tot_real)}")
    lines.append(f"*Σύνολο net flow σήμερα:* ${_format_amount(tot_flow)}")
    if _nonzero(tot_unrl):
        lines.append(f"*Σύνολο unreal (open τώρα):* ${_format_amount(tot_unrl)}")
    return "\n".join(lines)

# ---------- Totals (today|month|all) ----------
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
        except:
            pass
    else:
        try:
            for fn in os.listdir(DATA_DIR):
                if fn.startswith("transactions_") and fn.endswith(".json"):
                    files.append(fn)
        except:
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

def format_totals(scope: str):
    scope = (scope or "all").lower()
    rows = aggregate_per_asset(_load_entries_for_totals(scope))
    if not rows:
        return f"📊 Totals per Asset — {scope.capitalize()}: (no data)"
    lines = [f"📊 Totals per Asset — {scope.capitalize()}:"]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i}. {escape_md(r['asset'])}  "
            f"IN: {_format_amount(r['in_qty'])} (${_format_amount(r['in_usd'])}) | "
            f"OUT: {_format_amount(r['out_qty'])} (${_format_amount(r['out_usd'])}) | "
            f"NET: {_format_amount(r.get('net_qty', 0))} (${_format_amount(r.get('net_usd', 0))}) | "
            f"TX: {int(r.get('tx_count', 0))} | REAL: ${_format_amount(r['realized_usd'])}"
        )
    totals_line = f"\nΣύνολο realized: ${_format_amount(sum(float(x['realized_usd']) for x in rows))}"
    lines.append(totals_line)
    return "\n".join(lines)

# ---------- Wallet monitor loop ----------
def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    last_native_hashes = set()
    last_token_hashes = set()
    while not shutdown_event.is_set():
        try:
            for tx in fetch_latest_wallet_txs(limit=25):
                h = tx.get("hash")
                if h and h not in last_native_hashes:
                    last_native_hashes.add(h)
                    handle_native_tx(tx)
            for t in fetch_latest_token_txs(limit=100):
                h = t.get("hash")
                if h and h not in last_token_hashes:
                    last_token_hashes.add(h)
                    handle_erc20_tx(t)
            _replay_today_cost_basis()
        except Exception as e:
            log.exception("wallet monitor error: %s", e)
        for _ in range(WALLET_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1 + random.uniform(0, 0.3))

# ---------- Telegram long-poll ----------
import requests, math

def _load_offset():
    obj = read_json(OFFSET_PATH, default=None)
    if isinstance(obj, dict):
        return obj.get("offset")
    return None

def _save_offset(offset):
    try:
        write_json(OFFSET_PATH, {"offset": offset})
    except:
        pass

def _tg_api(method: str, **params):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r = requests.get(url, params=params, timeout=50)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        log.debug("tg api error %s: %s", method, e)
    return None

def telegram_long_poll_loop():
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; telegram loop disabled.")
        return
    offset = _load_offset()
    send_telegram("🤖 Telegram command handler online.")
    backoff = 1.0
    while not shutdown_event.is_set():
        try:
            resp = _tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
            if not resp or not resp.get("ok"):
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            backoff = 1.0
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                _save_offset(offset)
                msg = upd.get("message") or {}
                chat_id = str(((msg.get("chat") or {}).get("id") or ""))
                if TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID) != chat_id:
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                _handle_command(text)
        except Exception as e:
            log.debug("telegram poll error: %s", e)
            time.sleep(backoff + random.uniform(0, 0.5))
            backoff = min(backoff * 2, 30)

# ---------- Commands ----------
def _handle_command(text: str):
    t = text.strip()
    low = t.lower()
    if low.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
    elif low.startswith("/diag"):
        send_telegram(
            "🔧 Diagnostics\n"
            f"WALLETADDRESS: {escape_md(WALLET_ADDRESS)}\n"
            f"CRONOSRPCURL set: {bool(CRONOS_RPC_URL)}\n"
            f"Etherscan key: {bool(ETHERSCAN_API)}\n"
            f"LOGSCANBLOCKS={LOG_SCAN_BLOCKS} LOGSCANCHUNK={LOG_SCAN_CHUNK}\n"
            f"TZ={escape_md(TZ)} INTRADAYHOURS={INTRADAY_HOURS} EOD={EOD_HOUR:02d}:{EOD_MINUTE:02d}\n"
            f"Alerts every: {ALERTS_INTERVAL_MIN}m | Pump/Dump: {PUMP_ALERT_24H_PCT}/{DUMP_ALERT_24H_PCT}\n"
            f"Tracked pairs: {escape_md(', '.join(sorted(_tracked_pairs)) or '(none)')}"
        )
    elif low.startswith("/rescan"):
        cnt = 0
        try:
            tokens = discover_wallet_tokens()
        except Exception as e:
            tokens = []
            log.warning("Rescan error: %s", e)
        if tokens:
            with BAL_LOCK:
                for token in tokens:
                    addr = token.get("token_addr") or ""
                    bal = Decimal(str(token.get("balance") or 0.0))
                    sym = token.get("symbol") or (addr[:8].upper() if addr else None)
                    dec = token.get("decimals") or 18
                    if bal > Decimal("0"):
                        _token_balances[addr if addr else sym] = bal
                        _token_meta[addr if addr else sym] = {"symbol": sym or "", "decimals": dec}
                        cnt += 1
        send_telegram(f"🔄 Rescan done. Positive tokens: {cnt}")
    elif low.startswith("/holdings") or low.startswith("/show_wallet_assets") or low.startswith("/showwalletassets") or low == "/show":
        try:
            snapshot = get_wallet_snapshot()
            msg = format_holdings(snapshot)
            send_telegram(msg)
        except Exception as e:
            send_telegram(f"❌ Error fetching holdings:\n`{escape_md(str(e))}`")
    elif low.startswith("/dailysum") or low.startswith("/showdaily"):
        send_telegram(_format_daily_sum_message())
    elif low.startswith("/report"):
        send_telegram(build_day_report_text())
    elif low.startswith("/totals"):
        parts = low.split()
        scope = "all"
        if len(parts) > 1 and parts[1] in ("today", "month", "all"):
            scope = parts[1]
        send_telegram(format_totals(scope))
    elif low.startswith("/totalstoday"):
        send_telegram(format_totals("today"))
    elif low.startswith("/totalsmonth"):
        send_telegram(format_totals("month"))
    elif low.startswith("/pnl"):
        parts = low.split()
        scope = parts[1] if len(parts) > 1 and parts[1] in ("today", "month", "all") else "all"
        send_telegram(format_totals(scope))
    elif low.startswith("/watch "):
        try:
            _, rest = low.split(" ", 1)
            if rest.startswith("add "):
                pair = rest.split(" ", 1)[1].strip().lower()
                if pair.startswith("cronos/"):
                    ensure_tracking_pair("cronos", pair.split("/", 1)[1])
                    send_telegram(f"👁 Added {escape_md(pair)}")
                else:
                    send_telegram("Use format cronos/<pairAddress>")
            elif rest.startswith("rm "):
                pair = rest.split(" ", 1)[1].strip().lower()
                if pair in _tracked_pairs:
                    _tracked_pairs.remove(pair)
                    send_telegram(f"🗑 Removed {escape_md(pair)}")
                else:
                    send_telegram("Pair not tracked.")
            elif rest.strip() == "list":
                send_telegram("👁 Tracked:\n" + escape_md("\n".join(sorted(_tracked_pairs))) if _tracked_pairs else "None.")
            else:
                send_telegram("Usage: /watch add <cronos/pair> | /watch rm <cronos/pair> | /watch list")
        except Exception as e:
            send_telegram(f"Watch error: {escape_md(str(e))}")
    else:
        send_telegram("❓ Commands: /status /diag /rescan /holdings /show /dailysum /report /totals [today|month|all] /totalstoday /totalsmonth /pnl [scope] /watch ...")

# ---------- Schedulers (Intraday/EOD) ----------
def _scheduler_loop():
    global _last_intraday_sent
    send_telegram("⏱ Scheduler online (intraday/EOD).")
    while not shutdown_event.is_set():
        try:
            now = now_dt()
            if _last_intraday_sent <= 0 or (time.time() - _last_intraday_sent) >= INTRADAY_HOURS * 3600:
                send_telegram(_format_daily_sum_message())
                _last_intraday_sent = time.time()
            if now.hour == EOD_HOUR and now.minute == EOD_MINUTE:
                send_telegram(build_day_report_text())
                time.sleep(65)
        except Exception as e:
            log.debug("scheduler error: %s", e)
        for _ in range(20):
            if shutdown_event.is_set():
                break
            time.sleep(3)

# ---------- Main ----------
def _graceful_exit(signum, frame):
    try:
        send_telegram("🛑 Shutting down.")
    except:
        pass
    shutdown_event.set()

def main():
    load_ath()
    send_telegram("🟢 Starting Cronos DeFi Sentinel.")
    threads = [
        threading.Thread(target=discovery_loop, name="discovery"),
        threading.Thread(target=wallet_monitor_loop, name="wallet"),
        threading.Thread(target=monitor_tracked_pairs_loop, name="dex"),
        threading.Thread(target=alerts_monitor_loop, name="alerts"),
        threading.Thread(target=guard_monitor_loop, name="guard"),
        threading.Thread(target=telegram_long_poll_loop, name="telegram"),
        threading.Thread(target=_scheduler_loop, name="scheduler"),
    ]
    for t in threads:
        t.start()
    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    finally:
        for t in threads:
            t.join(timeout=5)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log.exception("fatal: %s", e)
        try:
            send_telegram(f"💥 Fatal error: {escape_md(str(e))}")
        except:
            pass
        sys.exit(1)
