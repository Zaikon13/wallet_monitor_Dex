# core/holdings.py
# Modular holdings snapshot for /holdings (compatible with legacy imports)
# - Provides set_runtime_refs(...) to satisfy older main.py imports
# - Exposes get_wallet_snapshot() and format_snapshot_lines()
# - Prices via core.pricing.get_price_usd (Dexscreener + history fallback)
# - CRO is always present; RECEIPT_SYMBOLS (e.g., TCRO) reported separately

import os
import json
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple, Optional

from zoneinfo import ZoneInfo
from core.pricing import get_price_usd


# ---------- Config / Paths ----------
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Europe/Athens"))
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
RECEIPT_SYMBOLS = {
    s.strip().upper() for s in (os.getenv("RECEIPT_SYMBOLS", "TCRO") or "").split(",") if s.strip()
}

# Runtime refs (OPTIONAL; set by main.py in some versions)
_RUNTIME_QTY: Optional[dict] = None
_RUNTIME_COST: Optional[dict] = None
_RUNTIME_META: Optional[dict] = None
_RUNTIME_BAL: Optional[dict] = None


# ---------- Public compatibility API ----------
def set_runtime_refs(
    *,
    position_qty: Optional[dict] = None,
    position_cost: Optional[dict] = None,
    token_meta: Optional[dict] = None,
    token_balances: Optional[dict] = None,
) -> None:
    """
    Older main.py versions import this to inject live runtime state.
    This module does not REQUIRE these refs, but we accept them for compatibility.
    """
    global _RUNTIME_QTY, _RUNTIME_COST, _RUNTIME_META, _RUNTIME_BAL
    _RUNTIME_QTY = position_qty
    _RUNTIME_COST = position_cost
    _RUNTIME_META = token_meta
    _RUNTIME_BAL = token_balances


def get_wallet_snapshot() -> Tuple[float, List[dict], float, List[dict]]:
    """
    Returns (total_usd, breakdown, unrealized_usd, receipts)
    - breakdown rows: {"token","token_addr","amount","price_usd","usd_value"}
    """
    pos_qty, pos_cost, key_to_symbol = _rebuild_open_positions_from_ledger()

    # If runtime refs are provided, optionally overlay CRO balance price/unrealized ONLY
    # to keep behavior conservative and avoid double counting quantities from live scans.
    # We DO NOT add runtime quantities here—ledger is the source of truth for /holdings.
    total_usd = 0.0
    unrealized = 0.0
    breakdown: List[dict] = []
    receipts: List[dict] = []

    # Ensure CRO always present
    if "CRO" not in pos_qty:
        pos_qty["CRO"] = 0.0
        pos_cost["CRO"] = 0.0
        key_to_symbol["CRO"] = "CRO"

    for key in list(pos_qty.keys()):
        qty = float(pos_qty.get(key, 0.0))
        # Keep CRO even if zero; skip other zeros
        if qty <= 1e-12 and key != "CRO":
            continue

        sym = _symbol_for_key(key, key_to_symbol)
        is_receipt = sym in RECEIPT_SYMBOLS or sym == "TCRO"

        price = _live_price_for_key(key, sym)
        usd_value = qty * (price or 0.0)

        cost = float(pos_cost.get(key, 0.0))
        unrl = 0.0
        if qty > 1e-12 and price and price > 0:
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

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    receipts.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    return float(total_usd), breakdown, float(unrealized), receipts


def format_snapshot_lines(
    total_usd: float,
    breakdown: List[dict],
    unrealized_usd: float,
    receipts: List[dict],
) -> str:
    """
    Legacy formatter kept here for full backwards compatibility with some main.py versions
    that import it from core.holdings. (Your project now also has telegram/formatters.py)
    """
    if not breakdown and not receipts:
        return "📦 Κενά holdings."

    def _fmt_amount(a) -> str:
        try:
            a = float(a)
        except Exception:
            return str(a)
        if abs(a) >= 1:
            return f"{a:,.4f}"
        if abs(a) >= 0.0001:
            return f"{a:.6f}"
        return f"{a:.8f}"

    def _fmt_price(p) -> str:
        try:
            p = float(p)
        except Exception:
            return str(p)
        if p >= 1:
            return f"{p:,.6f}"
        if p >= 0.01:
            return f"{p:.6f}"
        if p >= 1e-6:
            return f"{p:.8f}"
        return f"{p:.10f}"

    lines = ["*📦 Holdings (merged):*"]
    for b in breakdown or []:
        token = b.get("token") or "?"
        amt = b.get("amount", 0.0)
        prc = b.get("price_usd", 0.0)
        usd = b.get("usd_value", 0.0)
        lines.append(f"• {token}: {_fmt_amount(amt)}  @ ${_fmt_price(prc)}  = ${_fmt_amount(usd)}")

    if receipts:
        lines.append("\n*Receipts:*")
        for r in receipts:
            token = r.get("token") or "?"
            amt = r.get("amount", 0.0)
            prc = r.get("price_usd", 0.0)
            usd = r.get("usd_value", 0.0)
            lines.append(f"• {token}: {_fmt_amount(amt)}  (@ ${_fmt_price(prc)} → ${_fmt_amount(usd)})")

    lines.append(f"\nΣύνολο: ${_fmt_amount(total_usd)}")
    if abs(float(unrealized_usd or 0.0)) > 1e-12:
        lines.append(f"Unrealized: ${_fmt_amount(unrealized_usd)}")
    return "\n".join(lines)


# ---------- Internal helpers ----------
def _now_dt() -> datetime:
    return datetime.now(LOCAL_TZ)

def _ymd(dt: Optional[datetime] = None) -> str:
    return (dt or _now_dt()).strftime("%Y-%m-%d")

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

def _rebuild_open_positions_from_ledger() -> Tuple[Dict[str, float], Dict[str, float], Dict[str, str]]:
    """
    Build open positions strictly from ledger files (no RPC merge to avoid double counting).
    Returns:
      pos_qty:  key -> open qty   (key: 'CRO' or 0x<addr> or SYMBOL)
      pos_cost: key -> open cost
      key_to_symbol: key -> display symbol
    """
    pos_qty: Dict[str, float] = defaultdict(float)
    pos_cost: Dict[str, float] = defaultdict(float)
    key_to_symbol: Dict[str, str] = {}

    def _update(key: str, sym: str, signed_amount: float, price_usd: float):
        # Record symbol for display
        if sym:
            key_to_symbol[key] = sym
        qty = pos_qty[key]
        cost = pos_cost[key]
        if signed_amount > 1e-12:
            pos_qty[key] = qty + signed_amount
            pos_cost[key] = cost + signed_amount * (price_usd or 0.0)
        elif signed_amount < -1e-12:
            sell_qty = min(-signed_amount, qty) if qty > 1e-12 else 0.0
            avg_cost = (cost / qty) if qty > 1e-12 else (price_usd or 0.0)
            pos_qty[key] = max(0.0, qty - sell_qty)
            pos_cost[key] = max(0.0, cost - avg_cost * sell_qty)

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

            key = addr if (addr and addr.startswith("0x")) else (symU or sym_raw or "?")
            _update(key, symU or sym_raw or "?", amt, prc)

    # Normalize near-zeros
    for k, v in list(pos_qty.items()):
        if abs(v) < 1e-10:
            pos_qty[k] = 0.0

    return pos_qty, pos_cost, key_to_symbol

def _symbol_for_key(key: str, key_to_symbol: Dict[str, str]) -> str:
    if isinstance(key, str) and key.startswith("0x"):
        sym = key_to_symbol.get(key)
        return sym if sym else key[:8].upper()
    return (key_to_symbol.get(key) or key or "?").upper()

def _live_price_for_key(key: str, sym_hint: str) -> float:
    try:
        if isinstance(key, str) and key.startswith("0x"):
            p = get_price_usd(key)
            if p:
                return float(p)
        p = get_price_usd(sym_hint or key)
        return float(p or 0.0)
    except Exception:
        return 0.0


# ---------- Optional convenience wrapper ----------
def compute_holdings_merged() -> Tuple[float, List[dict], float, List[dict]]:
    """
    Convenience alias for older code paths that expect this name from core.holdings.
    Delegates to get_wallet_snapshot().
    """
    return get_wallet_snapshot()
