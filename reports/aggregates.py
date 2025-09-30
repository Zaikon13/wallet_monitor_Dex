# -*- coding: utf-8 -*-
"""
Aggregate helpers for totals, PnL and day reports.

Public API
----------
- load_remaining_cost_basis() -> dict[str, Decimal]
- compute_unrealized_from_snapshot(snapshot) -> dict[symbol, Decimal]
- day_totals(day: str) -> dict[str, Decimal]
- month_to_date_totals(yyyy_mm: str) -> dict[str, Decimal]
- build_daily_summary(day: str) -> str
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from core.tz import ymd, now_gr, month_prefix
from reports.ledger import (
    LEDGER_DIR,  # type: ignore[attr-defined]
    list_days,
    read_ledger,
)

# ---------------------------
# Decimal helpers
# ---------------------------
def _D(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


# ---------------------------
# Cost basis (remaining) loader
# ---------------------------
def _cost_basis_file() -> Path:
    return Path(os.getenv("LEDGER_DIR", "./.ledger")).expanduser() / "cost_basis.json"


def load_remaining_cost_basis() -> Dict[str, Decimal]:
    """
    Read remaining FIFO cost basis per asset from cost_basis.json, summing lots.
    Returns mapping: symbol -> remaining_cost_usd (Decimal)
    """
    path = _cost_basis_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    basis = data.get("basis") or {}
    out: Dict[str, Decimal] = {}
    for sym, lots in basis.items():
        rem = Decimal("0")
        for lot in lots or []:
            rem += _D(lot.get("usd"))
        out[sym.upper()] = rem
    return out


def compute_unrealized_from_snapshot(snapshot: Dict[str, Dict[str, str | None]]) -> Dict[str, Decimal]:
    """
    Given a holdings snapshot:
      {SYM: {"qty": "…", "usd": "…", "price_usd": "..."}}
    compute unrealized PnL per asset as: current_value_usd - remaining_cost_basis_usd
    """
    remaining = load_remaining_cost_basis()
    out: Dict[str, Decimal] = {}
    for sym, row in (snapshot or {}).items():
        cur = _D(row.get("usd"))
        rem = remaining.get(sym.upper(), Decimal("0"))
        out[sym.upper()] = cur - rem
    return out


# ---------------------------
# Totals aggregation
# ---------------------------
def _iter_month_days(yyyy_mm: str) -> Iterable[str]:
    # yyyy_mm like "2025-09"
    for d in list_days():
        if d.startswith(yyyy_mm + "-"):
            yield d


def _sum_fields(entries: Iterable[Dict[str, Any]]) -> Tuple[Decimal, Decimal, Decimal, Decimal]:
    """
    Returns (in_usd, out_usd, fee_usd, realized)
    """
    _in = Decimal("0")
    _out = Decimal("0")
    _fee = Decimal("0")
    _real = Decimal("0")
    for e in entries:
        side = str(e.get("side") or "").upper()
        usd = _D(e.get("usd"))
        fee = _D(e.get("fee_usd"))
        rv = _D(e.get("realized_usd"))
        if side == "IN":
            _in += usd
            _fee += fee
        elif side == "OUT":
            _out += usd
            _fee += fee
            _real += rv
    return _in, _out, _fee, _real


def day_totals(day: str | None = None) -> Dict[str, Decimal]:
    d = day or ymd()
    entries = read_ledger(d)
    _in, _out, _fee, _real = _sum_fields(entries)
    net = _in - _out - _fee
    return {
        "in": _in,
        "out": _out,
        "fee": _fee,
        "net": net,
        "realized": _real,
    }


def month_to_date_totals(yyyy_mm: str | None = None) -> Dict[str, Decimal]:
    mm = yyyy_mm or month_prefix()
    _in = _out = _fee = _real = Decimal("0")
    for d in _iter_month_days(mm):
        entries = read_ledger(d)
        a, b, c, r = _sum_fields(entries)
        _in += a
        _out += b
        _fee += c
        _real += r
    net = _in - _out - _fee
    return {
        "in": _in,
        "out": _out,
        "fee": _fee,
        "net": net,
        "realized": _real,
    }


# ---------------------------
# Daily summary
# ---------------------------
def _fmt_dec(val: Decimal) -> str:
    if val == 0:
        return "0"
    if abs(val) >= 1:
        return f"{val:,.2f}"
    return f"{val:.6f}"


def build_daily_summary(day: str | None = None) -> str:
    d = day or ymd()
    t = day_totals(d)
    mm = month_to_date_totals()
    lines = [
        f"📅 Daily — {d}",
        f"IN:  ${_fmt_dec(t['in'])}",
        f"OUT: ${_fmt_dec(t['out'])}",
        f"FEE: ${_fmt_dec(t['fee'])}",
        f"NET: ${_fmt_dec(t['net'])}",
        f"Realized: ${_fmt_dec(t['realized'])}",
        "",
        f"📆 MTD — {month_prefix()}",
        f"IN:  ${_fmt_dec(mm['in'])}",
        f"OUT: ${_fmt_dec(mm['out'])}",
        f"FEE: ${_fmt_dec(mm['fee'])}",
        f"NET: ${_fmt_dec(mm['net'])}",
        f"Realized: ${_fmt_dec(mm['realized'])}",
    ]
    return "\n".join(lines)
