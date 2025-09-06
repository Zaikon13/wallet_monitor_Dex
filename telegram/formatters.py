# telegram/formatters.py
from __future__ import annotations
from typing import List, Dict, Any
from decimal import Decimal


def _fmt_qty(x: Decimal) -> str:
    # μέχρι 4 δεκαδικά για qty
    return f"{x.normalize():f}".rstrip("0").rstrip(".") if x % 1 else f"{int(x)}"


def _fmt_usd(x: Decimal) -> str:
    # 6 δεκαδικά (όπως στα δικά σου reports)
    sign = "-" if x < 0 else ""
    y = abs(x)
    return f"{sign}${y:.6f}"


def format_per_asset_totals(scope: str, rows: List[Dict[str, Any]]) -> str:
    title = {
        "today": "Today",
        "month": "This Month",
        "all": "All Time",
        "all_time": "All Time",
    }.get(scope, scope)

    lines = [f"📊 Totals per Asset — {title}:"]
    if not rows:
        lines.append("  (no data)")
        return "\n".join(lines)

    for i, r in enumerate(rows, 1):
        lines.append(
            f" {i}. {r['asset']}"
            f"  IN: {_fmt_qty(r['in_qty'])} ({_fmt_usd(r['in_usd'])})"
            f" | OUT: {_fmt_qty(r['out_qty'])} ({_fmt_usd(r['out_usd'])})"
            f" | NET: {_fmt_qty(r['net_qty'])} ({_fmt_usd(r['net_usd'])})"
            f" | Realized: {_fmt_usd(r['realized_usd'])}"
            f" | TXs: {r['tx_count']}"
        )
    return "\n".join(lines)
