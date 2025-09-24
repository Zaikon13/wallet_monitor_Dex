# reports/ledger.py
"""
Ledger persistence & cost-basis tracking.
- Append new transactions to daily ledger
- Update/replay cost basis (FIFO)
"""

import os
import json
import logging
from decimal import Decimal
from typing import Dict, Any, List

from core.tz import ymd

DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)


def _ledger_path(date_str: str) -> str:
    return os.path.join(DATA_DIR, f"ledger_{date_str}.json")


def append_ledger(entry: Dict[str, Any], date_str: str | None = None) -> None:
    """
    Append a single ledger entry to today's file.
    Entry should be JSON-serializable.
    """
    if date_str is None:
        date_str = ymd()
    path = _ledger_path(date_str)
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = []
        data.append(entry)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logging.exception(f"Failed to append ledger entry: {e}")


def load_ledger(date_str: str | None = None) -> List[Dict[str, Any]]:
    """
    Load all ledger entries for given day.
    """
    if date_str is None:
        date_str = ymd()
    path = _ledger_path(date_str)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.exception(f"Failed to load ledger for {date_str}: {e}")
        return []


def update_cost_basis(ledger_entries: List[Dict[str, Any]]) -> Dict[str, Decimal]:
    """
    Recompute cost basis per symbol using FIFO.
    Returns {symbol: Decimal(cost_basis)}.
    """
    basis: Dict[str, Decimal] = {}
    qty: Dict[str, Decimal] = {}

    for entry in ledger_entries:
        sym = entry.get("symbol")
        amt = Decimal(str(entry.get("amount", "0")))
        usd = Decimal(str(entry.get("usd_value", "0")))
        if sym is None:
            continue

        if amt > 0:
            # Buy: increase basis and qty
            prev_qty = qty.get(sym, Decimal("0"))
            prev_basis = basis.get(sym, Decimal("0"))
            qty[sym] = prev_qty + amt
            basis[sym] = prev_basis + usd
        else:
            # Sell: reduce basis proportionally
            prev_qty = qty.get(sym, Decimal("0"))
            prev_basis = basis.get(sym, Decimal("0"))
            sell_qty = -amt
            if prev_qty > 0:
                ratio = sell_qty / prev_qty
                basis[sym] = prev_basis * (1 - ratio)
                qty[sym] = prev_qty - sell_qty
            else:
                # Selling without holdings, log but continue
                logging.warning(f"Selling {sell_qty} {sym} without qty in ledger")

    return basis


def replay_cost_basis_over_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Replay cost basis calculation and attach running basis to each entry.
    """
    basis: Dict[str, Decimal] = {}
    qty: Dict[str, Decimal] = {}

    out: List[Dict[str, Any]] = []

    for entry in entries:
        sym = entry.get("symbol")
        amt = Decimal(str(entry.get("amount", "0")))
        usd = Decimal(str(entry.get("usd_value", "0")))

        if sym is None:
            out.append(entry)
            continue

        if amt > 0:
            prev_qty = qty.get(sym, Decimal("0"))
            prev_basis = basis.get(sym, Decimal("0"))
            qty[sym] = prev_qty + amt
            basis[sym] = prev_basis + usd
        else:
            prev_qty = qty.get(sym, Decimal("0"))
            prev_basis = basis.get(sym, Decimal("0"))
            sell_qty = -amt
            if prev_qty > 0:
                ratio = sell_qty / prev_qty
                basis[sym] = prev_basis * (1 - ratio)
                qty[sym] = prev_qty - sell_qty

        enriched = dict(entry)
        enriched["running_basis"] = str(basis.get(sym, Decimal("0")))
        enriched["running_qty"] = str(qty.get(sym, Decimal("0")))
        out.append(enriched)

    return out
