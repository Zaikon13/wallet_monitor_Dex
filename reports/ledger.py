# -*- coding: utf-8 -*-
"""
Ledger helpers:
- append_ledger(entry)
- update_cost_basis(positions_qty, positions_cost, key, delta_qty, price, eps)
- replay_cost_basis_over_entries(positions_qty, positions_cost, entries, eps)
File layout (per day):
{
  "date": "YYYY-MM-DD",
  "entries": [ { ... } ],
  "net_usd_flow": 0.0,
  "realized_pnl": 0.0
}
"""
import os
import json
from datetime import datetime
from typing import Dict, List, Any, Tuple
from collections import defaultdict

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)

def _ymd(dt: datetime | None = None) -> str:
    dt = dt or datetime.now()
    return dt.strftime("%Y-%m-%d")

def _today_path() -> str:
    return os.path.join(DATA_DIR, f"transactions_{_ymd()}.json")

def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path: str, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def append_ledger(entry: Dict[str, Any]) -> None:
    """
    Append a normalized entry to today's file and update aggregates.
    Expected entry keys (as produced by main.py handlers):
    time, txhash, type, token, token_addr, amount, price_usd, usd_value, realized_pnl, from, to
    """
    path = _today_path()
    data = _read_json(path, default={"date": _ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    if not isinstance(data, dict):
        data = {"date": _ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0}

    e = dict(entry or {})
    # normalize
    e.setdefault("time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    e.setdefault("txhash", None)
    e.setdefault("type", "erc20")
    e.setdefault("token", "?")
    e.setdefault("token_addr", None)
    e["amount"] = float(e.get("amount") or 0.0)
    e["price_usd"] = float(e.get("price_usd") or 0.0)
    e["usd_value"] = float(e.get("usd_value") or 0.0)
    e["realized_pnl"] = float(e.get("realized_pnl") or 0.0)

    data["entries"].append(e)
    data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + e["usd_value"]
    data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + e["realized_pnl"]

    _write_json(path, data)

def update_cost_basis(positions_qty: Dict[str, float],
                      positions_cost: Dict[str, float],
                      key: str,
                      delta_qty: float,
                      price_usd: float,
                      eps: float = 1e-12) -> float:
    """
    FIFO-like average cost update (single bucket avg).
    Returns realized PnL for this trade (could be 0).
    - Buy: qty += q ; cost += q*price
    - Sell: realize (price - avg_cost) * sold_qty ; reduce qty/cost
    """
    if not key:
        return 0.0
    q = float(positions_qty.get(key, 0.0))
    c = float(positions_cost.get(key, 0.0))
    p = float(price_usd or 0.0)
    realized = 0.0

    if delta_qty > eps:
        # BUY
        positions_qty[key] = q + delta_qty
        positions_cost[key] = c + delta_qty * p
    elif delta_qty < -eps:
        # SELL
        sell_qty = min(-delta_qty, max(0.0, q))
        avg_cost = (c / q) if q > eps else p
        realized = (p - avg_cost) * sell_qty
        positions_qty[key] = max(0.0, q - sell_qty)
        positions_cost[key] = max(0.0, c - avg_cost * sell_qty)

    return float(realized)

def replay_cost_basis_over_entries(positions_qty: Dict[str, float],
                                   positions_cost: Dict[str, float],
                                   entries: List[Dict[str, Any]],
                                   eps: float = 1e-12) -> float:
    """
    Rebuild open positions & realized PnL by replaying a list of entries in order.
    Mutates positions_* dicts, returns total realized PnL in the replay.
    """
    positions_qty.clear()
    positions_cost.clear()
    total_realized = 0.0

    # sort by time if possible
    def _parse_ts(e):
        t = str(e.get("time") or "")
        try:
            return datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        except Exception:
            return datetime.min

    for e in sorted(entries or [], key=_parse_ts):
        sym = (e.get("token") or "").strip()
        addr = (e.get("token_addr") or "").strip().lower()
        key = addr if (addr.startswith("0x")) else (sym.upper() or "?")
        amt = float(e.get("amount") or 0.0)  # positive buy, negative sell (as produced by main.py)
        prc = float(e.get("price_usd") or 0.0)
        total_realized += update_cost_basis(positions_qty, positions_cost, key, amt, prc, eps=eps)

    return float(total_realized)
