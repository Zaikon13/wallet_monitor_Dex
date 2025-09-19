# core/holdings.py
"""
Wallet holdings & valuations (extracted from main.py)
- RPC + history-based valuations with USD pricing via Dexscreener.
- tCRO remains DISTINCT from CRO end-to-end (no merging).
- RECEIPT_SYMBOLS are excluded from main breakdown and returned separately.
- Exposes a tiny runtime binding so we DON'T own cost-basis maps here:
    set_runtime_refs(position_qty, position_cost, token_meta, token_balances)

Public API
----------
set_runtime_refs(pos_qty, pos_cost, token_meta, token_balances)
gather_all_known_token_contracts() -> set[str]
compute_holdings_usd_via_rpc() -> (total_usd, breakdown, unrealized_usd)
rebuild_open_positions_from_history() -> (pos_qty_map, pos_cost_map)
compute_holdings_usd_from_history_positions() -> (total_usd, breakdown, unrealized_usd)
compute_holdings_merged() -> (total_usd, breakdown, unrealized_usd, receipts)

Where:
- breakdown: list of {token, token_addr, amount, price_usd, usd_value}
- receipts : list with same shape as breakdown (for RECEIPT_SYMBOLS/tCRO)
"""

from __future__ import annotations

import os
import json
import time
import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Dict, Tuple, List, Optional, Iterable

from zoneinfo import ZoneInfo

# Project helpers
from core import rpc as rpc_mod  # defensive usage (some funcs may be absent)
from core.pricing import get_price_usd

# ---------- Environment / Constants ----------

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = ZoneInfo(TZ)

def now_dt(): return datetime.now(LOCAL_TZ)
def ymd(dt: Optional[datetime] = None) -> str: return (dt or now_dt()).strftime("%Y-%m-%d")

WALLET_ADDRESS = (os.getenv("WALLET_ADDRESS") or "").lower()
DATA_DIR       = os.getenv("DATA_DIR", "/app/data")

RECEIPT_SYMBOLS = set(
    s.strip().upper() for s in (os.getenv("RECEIPT_SYMBOLS", "TCRO")).split(",") if s.strip()
)

# How strict we treat near-zeros
EPSILON = 1e-12

# In-memory price cache from history (populated via _build_history_maps)
_HISTORY_LAST_PRICE: Dict[str, float] = {}

# Runtime refs (provided by main.py to avoid circular imports)
_pos_qty: Dict[str, float] = {}
_pos_cost: Dict[str, float] = {}
_token_meta: Dict[str, dict] = {}
_token_balances: Dict[str, float] = {}

def set_runtime_refs(
    position_qty: Dict[str, float],
    position_cost: Dict[str, float],
    token_meta: Dict[str, dict],
    token_balances: Dict[str, float],
) -> None:
    """
    Bind live dicts from main.py so we can compute unrealized PnL & show symbols.
    """
    global _pos_qty, _pos_cost, _token_meta, _token_balances
    _pos_qty = position_qty
    _pos_cost = position_cost
    _token_meta = token_meta
    _token_balances = token_balances


# ---------- File IO ----------

def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ---------- History maps & price hints ----------

def _build_history_maps() -> Dict[str, str]:
    """
    Walk all transactions_*.json and:
      - collect last seen prices per symbol/addr into _HISTORY_LAST_PRICE
      - build a 'symbol -> contract' map (dropping conflicts).
    """
    symbol_to_contract: Dict[str, str] = {}
    symbol_conflict = set()
    files: List[str] = []

    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"):
                files.append(fn)
    except Exception as ex:
        logging.exception("listdir data error: %s", ex)

    files.sort()
    for fn in files:
        data = _read_json(os.path.join(DATA_DIR, fn), default=None)
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
                    symbol_conflict.add(sym)
                else:
                    symbol_to_contract.setdefault(sym, addr)

    for s in symbol_conflict:
        symbol_to_contract.pop(s, None)

    return symbol_to_contract


def _history_price_fallback(query_key: str, symbol_hint: Optional[str] = None) -> Optional[float]:
    """
    Best-effort price if live Dexscreener lookup fails.
    """
    if not query_key:
        return None
    k = query_key.strip()
    if not k:
        return None

    if k.startswith("0x"):
        p = _HISTORY_LAST_PRICE.get(k)
        if p and p > 0:
            return p

    sym = (symbol_hint or k)
    p = _HISTORY_LAST_PRICE.get((sym or "").upper())
    if p and p > 0:
        return p

    if (sym or "").upper() == "CRO":
        p = _HISTORY_LAST_PRICE.get("CRO")
        if p and p > 0:
            return p

    return None


# ---------- Known contracts discovery ----------

def _safe_lower_hex(s: str) -> str:
    return (s or "").strip().lower()

def gather_all_known_token_contracts() -> set[str]:
    """
    Gather token contracts from:
      - runtime token_meta (already seen)
      - history files symbol->contract mapping
      - latest token txs via Etherscan (if available)
      - on-chain log discovery via RPC (if available)
      - TOKENS env (cronos/<addr> entries)
    """
    known: set[str] = set()

    # 1) From runtime token_meta
    for k in list(_token_meta.keys()):
        if isinstance(k, str) and k.startswith("0x"):
            known.add(_safe_lower_hex(k))

    # 2) From history
    symbol_to_contract = _build_history_maps()
    for addr in symbol_to_contract.values():
        if addr and addr.startswith("0x"):
            known.add(_safe_lower_hex(addr))

    # 3) Etherscan (optional)
    try:
        if hasattr(rpc_mod, "fetch_latest_token_txs"):
            for t in rpc_mod.fetch_latest_token_txs(limit=100) or []:
                addr = _safe_lower_hex(t.get("contractAddress"))
                if addr.startswith("0x"):
                    known.add(addr)
    except Exception:
        pass

    # 4) On-chain logs via RPC (optional)
    try:
        if hasattr(rpc_mod, "rpc_discover_token_contracts_by_logs"):
            rpc_found = rpc_mod.rpc_discover_token_contracts_by_logs(WALLET_ADDRESS, int(os.getenv("LOG_SCAN_BLOCKS", "120000")), int(os.getenv("LOG_SCAN_CHUNK", "5000")))
            known |= set(rpc_found or [])
    except Exception:
        pass

    # 5) TOKENS seeds
    for item in [x.strip().lower() for x in (os.getenv("TOKENS", "")).split(",") if x.strip()]:
        if item.startswith("cronos/"):
            _, addr = item.split("/", 1)
            if addr.startswith("0x"):
                known.add(_safe_lower_hex(addr))

    return known


# ---------- Pricing helpers ----------

PRICE_ALIASES = {"tcro": "cro"}  # normalize common receipt alias

def _price_for_symbol_or_addr(sym_or_addr: str, symbol_hint: Optional[str] = None) -> float:
    """
    Try live price via Dexscreener; fallback to last-known history price.
    """
    if not sym_or_addr:
        return 0.0

    key = PRICE_ALIASES.get(sym_or_addr.strip().lower(), sym_or_addr.strip().lower())
    price: Optional[float] = None

    try:
        price = get_price_usd(key)  # may accept either symbol or 0x...
    except Exception:
        price = None

    if price is None or not price or float(price) <= 0:
        hist = _history_price_fallback(sym_or_addr, symbol_hint=symbol_hint)
        if hist and hist > 0:
            price = float(hist)

    return float(price or 0.0)


# ---------- Holdings: RPC valuation ----------

def compute_holdings_usd_via_rpc() -> Tuple[float, List[dict], float]:
    """
    Use live RPC balances (native + ERC-20) and live prices.
    Returns total USD, breakdown list, and unrealized USD (against open positions in _pos_*).
    """
    total, breakdown, unrealized = 0.0, [], 0.0

    # Native CRO
    cro_amt = 0.0
    if hasattr(rpc_mod, "rpc_init") and hasattr(rpc_mod, "rpc_get_native_balance"):
        try:
            if rpc_mod.rpc_init():
                cro_amt = float(rpc_mod.rpc_get_native_balance(WALLET_ADDRESS) or 0.0)
        except Exception:
            cro_amt = 0.0

    if cro_amt > EPSILON:
        cro_price = _price_for_symbol_or_addr("CRO", "CRO")
        cro_val = cro_amt * cro_price
        total += cro_val
        breakdown.append({"token": "CRO", "token_addr": None, "amount": cro_amt, "price_usd": cro_price, "usd_value": cro_val})

        rem_qty = float(_pos_qty.get("CRO", 0.0))
        rem_cost = float(_pos_cost.get("CRO", 0.0))
        if rem_qty > EPSILON and cro_price > 0:
            unrealized += (cro_amt * cro_price - rem_cost)

    # ERC-20 via known contracts
    contracts = gather_all_known_token_contracts()
    for addr in sorted(list(contracts)):
        try:
            bal = 0.0
            sym, dec = None, 18
            if hasattr(rpc_mod, "rpc_get_erc20_balance") and hasattr(rpc_mod, "rpc_get_symbol_decimals"):
                bal = float(rpc_mod.rpc_get_erc20_balance(addr, WALLET_ADDRESS) or 0.0)
                sym, dec = rpc_mod.rpc_get_symbol_decimals(addr)
            if bal <= EPSILON:
                continue

            pr = _price_for_symbol_or_addr(addr, sym or addr)
            val = bal * pr
            total += val
            breakdown.append({"token": sym or addr[:8].upper(), "token_addr": addr, "amount": bal, "price_usd": pr, "usd_value": val})

            rem_qty = float(_pos_qty.get(addr, 0.0))
            rem_cost = float(_pos_cost.get(addr, 0.0))
            if rem_qty > EPSILON and pr > 0:
                unrealized += (bal * pr - rem_cost)
        except Exception:
            continue

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    return total, breakdown, unrealized


# ---------- Holdings: reconstruct open positions from history ----------

def rebuild_open_positions_from_history() -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    Rebuild open positions from ledger files (transactions_*.json) using a simple FIFO-ish model.
    Does NOT mutate the live runtime _pos_* maps; returns new maps.
    """
    pos_qty: Dict[str, float] = defaultdict(float)
    pos_cost: Dict[str, float] = defaultdict(float)
    symbol_to_contract = _build_history_maps()

    def _update(token_key: str, signed_amount: float, price_usd: float):
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

    files: List[str] = []
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"):
                files.append(fn)
    except Exception as ex:
        logging.exception("listdir data error: %s", ex)

    files.sort()
    for fn in files:
        data = _read_json(os.path.join(DATA_DIR, fn), default=None)
        if not isinstance(data, dict):
            continue
        for e in data.get("entries", []):
            sym_raw = (e.get("token") or "").strip()
            addr_raw = (e.get("token_addr") or "").strip().lower()
            amt = float(e.get("amount") or 0.0)
            pr = float(e.get("price_usd") or 0.0)

            if addr_raw and addr_raw.startswith("0x"):
                key = addr_raw
            else:
                mapped = symbol_to_contract.get(sym_raw) or symbol_to_contract.get((sym_raw or "").upper())
                key = mapped if (mapped and mapped.startswith("0x")) else ((sym_raw or "").upper() or "?")

            _update(key, amt, pr)

    for k, v in list(pos_qty.items()):
        if abs(v) < 1e-10:
            pos_qty[k] = 0.0

    return pos_qty, pos_cost


# ---------- Holdings: history-valued snapshot ----------

def _sym_for_key(key: str) -> str:
    if isinstance(key, str) and key.startswith("0x"):
        return (_token_meta.get(key, {}) or {}).get("symbol") or key[:8].upper()
    return str(key)

def _price_for_key(key: str, sym_hint: str) -> float:
    if isinstance(key, str) and key.startswith("0x"):
        p = _price_for_symbol_or_addr(key, sym_hint)
    else:
        p = _price_for_symbol_or_addr(sym_hint, sym_hint)
    return float(p or 0.0)

def compute_holdings_usd_from_history_positions() -> Tuple[float, List[dict], float]:
    """
    Use reconstructed open positions and current prices (with history fallback).
    """
    pos_qty, pos_cost = rebuild_open_positions_from_history()
    total, breakdown, unrealized = 0.0, [], 0.0

    for key, amt in pos_qty.items():
        amt = max(0.0, float(amt))
        if amt <= EPSILON:
            continue
        sym = _sym_for_key(key)
        p = _price_for_key(key, sym)
        v = amt * p
        total += v
        breakdown.append({
            "token": sym,
            "token_addr": key if (isinstance(key, str) and key.startswith("0x")) else None,
            "amount": amt,
            "price_usd": p,
            "usd_value": v,
        })
        cost = float(pos_cost.get(key, 0.0))
        if amt > EPSILON and p > 0:
            unrealized += (amt * p - cost)

    # Normalize only CRO symbol (never merge TCRO)
    for b in breakdown:
        if b["token"].upper() == "CRO" or b["token_addr"] is None:
            b["token"] = "CRO"

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    return total, breakdown, unrealized


# ---------- Merge (RPC vs History) with RECEIPT separation ----------

def compute_holdings_merged() -> Tuple[float, List[dict], float, List[dict]]:
    """
    Merge RPC and history-based holdings, preferring non-zero prices when available.
    Splits RECEIPT_SYMBOLS (and TCRO) into a separate list.
    """
    total_r, br_r, unrl_r = compute_holdings_usd_via_rpc()
    total_h, br_h, unrl_h = compute_holdings_usd_from_history_positions()

    def _key(b: dict) -> str:
        addr = b.get("token_addr")
        sym = (b.get("token") or "").upper()
        return addr.lower() if (isinstance(addr, str) and addr.startswith("0x")) else sym

    merged: Dict[str, dict] = {}

    def _add(b: dict) -> None:
        k = _key(b)
        if not k:
            return
        cur = merged.get(k, {
            "token": b["token"],
            "token_addr": b.get("token_addr"),
            "amount": 0.0,
            "price_usd": 0.0,
            "usd_value": 0.0,
        })
        cur["token"] = b["token"] or cur["token"]
        cur["token_addr"] = b.get("token_addr", cur.get("token_addr"))
        cur["amount"] += float(b.get("amount") or 0.0)
        pr = float(b.get("price_usd") or 0.0)
        if pr > 0:
            cur["price_usd"] = pr
        cur["usd_value"] = cur["amount"] * (cur["price_usd"] or 0.0)
        merged[k] = cur

    for b in br_h or []:
        _add(b)
    for b in br_r or []:
        _add(b)

    receipts: List[dict] = []
    breakdown: List[dict] = []
    total = 0.0
    for rec in merged.values():
        symU = (rec["token"] or "").upper()
        if symU in RECEIPT_SYMBOLS or symU == "TCRO":
            receipts.append(rec)
            continue
        if symU == "CRO":
            rec["token"] = "CRO"
        rec["usd_value"] = float(rec.get("amount", 0.0)) * float(rec.get("price_usd", 0.0) or 0.0)
        total += rec["usd_value"]
        breakdown.append(rec)

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    unrealized = unrl_r + unrl_h
    return total, breakdown, unrealized, receipts


# ---------- Convenience: plain wallet snapshot (symbol->amount,usd) ----------
# (Optional) If you want a simple per-symbol snapshot similar to your older helper.

def simple_snapshot(include_usd: bool = True) -> Dict[str, Dict[str, float]]:
    """
    Build a simple dict snapshot from merged holdings.
    {SYM: {"amount": float, "usd_value": float}}
    """
    _, breakdown, _, receipts = compute_holdings_merged()
    snap: Dict[str, Dict[str, float]] = {}
    def _add(item: dict):
        s = (item.get("token") or "").upper()
        if not s:
            return
        x = snap.setdefault(s, {"amount": 0.0, "usd_value": 0.0})
        x["amount"] += float(item.get("amount") or 0.0)
        if include_usd:
            x["usd_value"] += float(item.get("usd_value") or 0.0)

    for b in breakdown:
        _add(b)
    for r in receipts:
        _add(r)
    return snap


__all__ = [
    "set_runtime_refs",
    "gather_all_known_token_contracts",
    "compute_holdings_usd_via_rpc",
    "rebuild_open_positions_from_history",
    "compute_holdings_usd_from_history_positions",
    "compute_holdings_merged",
    "simple_snapshot",
]
