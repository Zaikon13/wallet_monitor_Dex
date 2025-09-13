# reports/ledger.py
# -*- coding: utf-8 -*-
"""
Ledger I/O + Cost-Basis helpers

Παρέχει:
- read_json(path, default)
- write_json(path, obj)
- data_file_for_date(date_str)
- data_file_for_today()
- append_ledger(entry)

- update_cost_basis(position_qty, position_cost, token_key, signed_amount, price_usd, eps)
  -> realized PnL (float) για το συγκεκριμένο trade

- replay_cost_basis_over_entries(position_qty, position_cost, entries_iterable, eps)
  -> συνολικό realized PnL (float) αφού κάνει replay των entries
"""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Iterable, Any

# ------------------------------------------------------------
# Config / Paths
# ------------------------------------------------------------

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = ZoneInfo(TZ)

# Επιτρέπει override μέσω ENV, αλλιώς default
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)

# Ledger write lock (thread-safe appends)
_LEDGER_LOCK = threading.Lock()

# ------------------------------------------------------------
# Small utils
# ------------------------------------------------------------

def ymd(dt: datetime | None = None) -> str:
    dt = dt or datetime.now(LOCAL_TZ)
    return dt.strftime("%Y-%m-%d")


def read_json(path: str, default: Any):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def data_file_for_date(date_str: str) -> str:
    return os.path.join(DATA_DIR, f"transactions_{date_str}.json")


def data_file_for_today() -> str:
    return data_file_for_date(ymd())


def _normalize_symbol(sym: str | None) -> str:
    """
    ΜΟΝΟ uppercase/trim — ΔΕΝ κάνουμε πλέον alias TCRO→CRO.
    Το receipt token (π.χ. TCRO) πρέπει να παραμείνει διακριτό σε όλο το pipeline.
    """
    s = (sym or "").strip().upper()
    return s or "?"


# ------------------------------------------------------------
# Ledger I/O
# ------------------------------------------------------------

def append_ledger(entry: Dict[str, Any]) -> None:
    """
    Προσθέτει entry στο σημερινό αρχείο και ενημερώνει net_usd_flow / realized_pnl.
    Thread-safe με _LEDGER_LOCK.
    Το entry αναμένεται να περιέχει: amount, price_usd, usd_value, realized_pnl, token, token_addr, time, ...
    """
    with _LEDGER_LOCK:
        path = data_file_for_today()
        payload = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})

        # κανονικοποίηση συμβόλου πριν γράψουμε (χωρίς alias TCRO→CRO)
        try:
            if "token" in entry:
                entry["token"] = _normalize_symbol(entry.get("token"))
        except Exception:
            pass

        if not isinstance(payload, dict):
            payload = {"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0}
        if "entries" not in payload or not isinstance(payload["entries"], list):
            payload["entries"] = []

        payload["entries"].append(entry)

        try:
            payload["net_usd_flow"] = float(payload.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
        except Exception:
            pass
        try:
            payload["realized_pnl"] = float(payload.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0))
        except Exception:
            pass

        write_json(path, payload)


# ------------------------------------------------------------
# Cost-basis (FIFO-like avg cost χωρίς lot tracking)
# ------------------------------------------------------------

def update_cost_basis(
    position_qty: Dict[str, float],
    position_cost: Dict[str, float],
    token_key: str,
    signed_amount: float,
    price_usd: float,
    eps: float = 1e-12,
) -> float:
    """
    Ενημερώνει τις δομές θέσης (qty/cost) και επιστρέφει realized PnL για το συγκεκριμένο trade.
    - θετικό amount => αγορά, αυξάνει qty & cost
    - αρνητικό amount => πώληση, μειώνει qty, αφαιρεί αναλογικό cost, υπολογίζει realized (sell - avg_cost)*qty_sold
    """
    qty = float(position_qty.get(token_key, 0.0))
    cost = float(position_cost.get(token_key, 0.0))
    realized = 0.0

    if signed_amount > eps:
        buy_qty = float(signed_amount)
        position_qty[token_key] = qty + buy_qty
        position_cost[token_key] = cost + buy_qty * float(price_usd or 0.0)

    elif signed_amount < -eps:
        sell_req = -float(signed_amount)
        if qty > eps:
            sell_qty = min(sell_req, qty)
            avg_cost = (cost / qty) if qty > eps else float(price_usd or 0.0)
            realized = (float(price_usd or 0.0) - avg_cost) * sell_qty
            position_qty[token_key] = qty - sell_qty
            position_cost[token_key] = max(0.0, cost - avg_cost * sell_qty)
        else:
            realized = 0.0  # πώληση χωρίς ποσότητα -> δεν αλλάζουμε κόστος

    # καθάρισμα κοντά στο 0
    if abs(position_qty.get(token_key, 0.0)) < eps:
        position_qty[token_key] = 0.0
    if abs(position_cost.get(token_key, 0.0)) < eps:
        position_cost[token_key] = 0.0

    return float(realized)


def _key_from_entry(e: Dict[str, Any]) -> str:
    """
    Παράγει το token_key από ένα ledger entry.
    Προτεραιότητα: token_addr (0x...) αλλιώς σύμβολο (ΧΩΡΙΣ alias TCRO→CRO).
    """
    addr = (e.get("token_addr") or "").strip().lower()
    if addr.startswith("0x"):
        return addr
    sym = _normalize_symbol(e.get("token"))
    return sym


def replay_cost_basis_over_entries(
    position_qty: Dict[str, float] | None,
    position_cost: Dict[str, float] | None,
    entries_iterable: Iterable[Dict[str, Any]],
    eps: float = 1e-12,
) -> float:
    """
    Κάνει replay μιας λίστας από entries και ενημερώνει τις δομές θέσης.
    Επιστρέφει το συνολικό realized PnL που προέκυψε από τα entries.
    """
    if position_qty is None:
        position_qty = defaultdict(float)
    if position_cost is None:
        position_cost = defaultdict(float)

    total_realized = 0.0
    for e in entries_iterable:
        try:
            amt = float(e.get("amount") or 0.0)
            pr = float(e.get("price_usd") or 0.0)
            key = _key_from_entry(e)
            realized = update_cost_basis(position_qty, position_cost, key, amt, pr, eps=eps)
            # προαιρετικά γράφουμε πίσω το realized στο entry (αν θέλουμε να το αποθηκεύσουμε αργότερα)
            e["realized_pnl"] = float(realized)
            total_realized += float(realized)
        except Exception:
            # συνεχίζουμε σιωπηλά για robustness
            continue

    return float(total_realized)


__all__ = [
    "read_json",
    "write_json",
    "data_file_for_date",
    "data_file_for_today",
    "append_ledger",
    "update_cost_basis",
    "replay_cost_basis_over_entries",
]
