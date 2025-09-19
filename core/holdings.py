# core/holdings.py
# Modular holdings snapshot for /holdings command
# - Reads ledger files from DATA_DIR and reconstructs open positions
# - Prices via core.pricing.get_price_usd
# - Keeps CRO always present and treats receipt symbols (e.g., TCRO) separately
# - Returns (total_usd, breakdown, unrealized_usd, receipts)

import os
import json
import math
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple, Optional

from core.pricing import get_price_usd  # uses Dexscreener + history fallback


# ---------- Config / Paths ----------
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Europe/Athens"))
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
RECEIPT_SYMBOLS = {
    s.strip().upper() for s in (os.getenv("RECEIPT_SYMBOLS", "TCRO") or "").split(",") if s.strip()
}


# ---------- Time helpers ----------
def now_dt() -> datetime:
    return datetime.now(LOCAL_TZ)

def ymd(dt: Optional[datetime] = None) -> str:
    return (dt or now_dt()).strftime("%Y-%m-%d")


# ---------- IO ----------
def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _list_ledger_files() -> List[str]:
    files = []
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"):
                files.append(os.path.join(DATA_DIR, fn))
    except Exception:
        pass
    files.sort()
    return files


# ---------- Math helpers ----------
EPS = 1e-12

def _nonzero(v: float, eps: float = EPS) -> bool:
    try:
        return abs(float(v)) > eps
    except Exception:
        return False


# ---------- Snapshot core ----------
def _rebuild_open_positions_from_ledger() -> Tuple[Dict[str, float], Dict[str, float], Dict[str, str]]:
    """
    Returns:
      pos_qty:  key -> open quantity  (key: 'CRO' or 0x<addr> or SYMBOL)
      pos_cost: key -> running cost basis for the open quantity (simple moving avg approach)
      key_to_symbol: key -> display symbol
    We avoid importing the project's reports.ledger here to keep this module standalone.
    """
    pos_qty: Dict[str, float] = defaultdict(float)
    pos_cost: Dict[str, float] = defaultdict(float)
    key_to_symbol: Dict[str, str] = {}

    def _update(key: str, sym: str, signed_amount: float, price_usd: float):
        # Keep last known symbol for display
        if sym:
            key_to_symbol[key] = sym

        qty = pos_qty[key]
        cost = pos_cost[key]

        if signed_amount > EPS:
            # Buy: increase qty and add to cost
            pos_qty[key] = qty + signed_amount
            pos_cost[key] = cost + signed_amount * (price_usd or 0.0)
        elif signed_amount < -EPS:
            # Sell/Out: reduce qty and proportionally reduce cost (avg cost)
            sell_qty = min(-signed_amount, qty) if qty > EPS else 0.0
            avg_cost = (cost / qty) if qty > EPS else (price_usd or 0.0)
            pos_qty[key] = max(0.0, qty - sell_qty)
            pos_cost[key] = max(0.0, cost - avg_cost * sell_qty)
        # else ~0: ignore

    for path in _list_ledger_files():
        data = _read_json(path, default=None)
        if not isinstance(data, dict):
            continue
        for e in data.get("entries", []):
            sym_raw = (e.get("token") or "").strip()
            symU = sym_raw.upper() if sym_raw else sym_raw
            addr = (e.get("token_addr") or "").strip().lower()
            amt = float(e.get("amount") or 0.0)
            prc = float(e.get("price_usd") or 0.0)

            # Key preference: contract address if present, else SYMBOL (upper)
            if addr and addr.startswith("0x"):
                key = addr
            else:
                key = symU or sym_raw or "?"

            _update(key, symU or sym_raw or "?", amt, prc)

    # Normalize near-zero to 0
    for k, v in list(pos_qty.items()):
        if abs(v) < 1e-10:
            pos_qty[k] = 0.0

    return pos_qty, pos_cost, key_to_symbol


def _symbol_for_key(key: str, key_to_symbol: Dict[str, str]) -> str:
    if isinstance(key, str) and key.startswith("0x"):
        # Display from meta if recorded; else short form
        sym = key_to_symbol.get(key)
        return sym if sym else key[:8].upper()
    # If it's CRO or other symbol
    return (key_to_symbol.get(key) or key or "?").upper()


def _live_price_for_key(key: str, sym_hint: str) -> float:
    """
    Resolve USD price for a key (contract or symbol).
    - Prefer contract lookup.
    - Fallback to symbol lookup.
    """
    try:
        if isinstance(key, str) and key.startswith("0x"):
            p = get_price_usd(key)
            if p:
                return float(p)
        # Symbol path (normalize tcro->cro aliasing happens inside pricing)
        p = get_price_usd(sym_hint or key)
        return float(p or 0.0)
    except Exception:
        return 0.0


def get_wallet_snapshot() -> Tuple[float, List[dict], float, List[dict]]:
    """
    Public API used by telegram/formatters.py (and optionally main.py):
      Returns (total_usd, breakdown, unrealized_usd, receipts)

      - breakdown: list of dicts:
          { "token": str, "token_addr": Optional[str], "amount": float,
            "price_usd": float, "usd_value": float }
      - receipts: same shape, but only receipt-like assets (TCRO etc.)
    """
    pos_qty, pos_cost, key_to_symbol = _rebuild_open_positions_from_ledger()

    total_usd = 0.0
    unrealized = 0.0
    breakdown: List[dict] = []
    receipts: List[dict] = []

    # Ensure CRO is always present (project rule)
    if "CRO" not in pos_qty:
        pos_qty["CRO"] = 0.0
        pos_cost["CRO"] = 0.0
        key_to_symbol["CRO"] = "CRO"

    all_keys = list(pos_qty.keys())
    for key in all_keys:
        qty = float(pos_qty.get(key, 0.0))
        if qty <= EPS and key != "CRO":
            # keep CRO even if zero; otherwise skip zero-qty assets
            continue

        sym = _symbol_for_key(key, key_to_symbol)
        # Keep TCRO (and any RECEIPT_SYMBOLS) separate from CRO
        is_receipt = sym in RECEIPT_SYMBOLS or sym == "TCRO"

        price = _live_price_for_key(key, sym)
        usd_value = qty * (price or 0.0)

        # Unrealized: value - open cost (if we have an open position)
        cost = float(pos_cost.get(key, 0.0))
        unrl = 0.0
        if qty > EPS and _nonzero(price):
            unrl = qty * price - cost

        row = {
            "token": "CRO" if sym == "CRO" else sym,
            "token_addr": key if (isinstance(key, str) and key.startswith("0x")) else None,
            "amount": qty,
            "price_usd": float(price or 0.0),
            "usd_value": float(usd_value or 0.0),
        }

        if is_receipt:
            receipts.append(row)
        else:
            breakdown.append(row)
            total_usd += row["usd_value"]
            unrealized += unrl

    # Sort by USD value desc for readability
    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    receipts.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)

    return float(total_usd), breakdown, float(unrealized), receipts
