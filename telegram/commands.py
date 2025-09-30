from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

from core.runtime_state import get_snapshot, get_state
from core.tz import ymd
from reports.aggregates import aggregate_per_asset, totals
from reports.day_report import build_day_report_text
from reports.ledger import iter_all_entries, read_ledger
from reports.weekly import build_weekly_report_text


# ---------- helpers ----------

def _fmt_decimal(value: Decimal) -> str:
    value = Decimal(value)
    if value == 0:
        return "0"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.6f}"


def _fmt_pct(value: Decimal) -> str:
    try:
        return f"{Decimal(value):.2f}%"
    except Exception:
        return "0%"


def _format_age(ts: Optional[float]) -> str:
    if not ts:
        return "never"
    delta = max(0.0, time.time() - ts)
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


# ---------- commands ----------

def handle_diag() -> str:
    state = get_state()
    wallet = os.getenv("WALLET_ADDRESS", "")
    lines: List[str] = ["Diagnostics:"]

    rpc_ok_ts = state.get("last_rpc_ok_ts")
    rpc_error = state.get("last_rpc_error")
    if rpc_ok_ts:
        rpc_line = f"RPC reachable: yes ({_format_age(rpc_ok_ts)})"
    elif rpc_error:
        rpc_line = f"RPC reachable: no (last error: {rpc_error})"
    else:
        rpc_line = "RPC reachable: unknown"
    lines.append(rpc_line)

    lines.append(f"Wallet configured: {'yes' if wallet else 'no'}")
    lines.append(f"Last tick: {_format_age(state.get('last_tick_ts'))}")
    lines.append(f"Last wallet poll: {_format_age(state.get('last_wallet_poll_ts'))}")

    queues = state.get("queue_sizes") or {}
    if queues:
        for name, size in sorted(queues.items()):
            lines.append(f"Queue {name}: {size}")
    else:
        lines.append("Queue sizes: n/a")

    cost_basis_ts = state.get("last_cost_basis_update")
    if cost_basis_ts:
        lines.append(f"Cost basis updated: {_format_age(cost_basis_ts)}")

    return "\n".join(lines)


def handle_status() -> str:
    state = get_state()
    snapshot = state.get("snapshot") or {}
    total = Decimal(state.get("snapshot_total_usd") or 0)

    lines: List[str] = ["Status:"]
    lines.append(f"Assets tracked: {len(snapshot)}")
    lines.append(f"Estimated total: ${_fmt_decimal(total)}")
    lines.append(f"Snapshot age: {_format_age(state.get('snapshot_ts'))}")
    lines.append(f"Last wallet poll: {_format_age(state.get('last_wallet_poll_ts'))}")

    return "\n".join(lines)


def handle_holdings(limit: int = 20) -> str:
    snapshot = get_snapshot()
    if not snapshot:
        return "No holdings snapshot available."

    items: List[tuple[str, Decimal, Decimal]] = []
    for symbol, data in snapshot.items():
        qty = Decimal(str(data.get("qty") or data.get("amount") or "0"))
        usd = Decimal(str(data.get("usd") or data.get("value_usd") or "0"))
        items.append((symbol.upper(), qty, usd))

    items.sort(key=lambda item: item[2], reverse=True)

    lines: List[str] = ["Holdings (top {limit}):".format(limit=limit)]
    for symbol, qty, usd in items[:limit]:
        lines.append(f" - {symbol:<6} {qty:>12,.4f} ≈ ${usd:,.2f}")

    if len(items) > limit:
        remaining = sum(usd for _, _, usd in items[limit:])
        lines.append(f"{len(items) - limit} more positions totaling ≈ ${remaining:,.2f}")

    total_usd = sum(usd for _, _, usd in items)
    lines.append("")
    lines.append(f"Total ≈ ${total_usd:,.2f}")
    return "\n".join(lines)


def handle_totals() -> str:
    entries = list(iter_all_entries())
    rows = aggregate_per_asset(entries)
    if not rows:
        return "Ledger is empty."

    totals_row = totals(rows)

    # base για ποσοστά κατανομής
    base_values = [
        max(
            abs(row.get("net_usd", Decimal("0"))),
            row.get("in_usd", Decimal("0")),
            row.get("out_usd", Decimal("0")),
        )
        for row in rows
    ]
    base_total = sum(base_values)
    if base_total <= 0:
        base_total = sum(abs(row.get("realized_usd", Decimal("0"))) for row in rows)

    rows.sort(key=lambda row: row.get("net_usd", Decimal("0")), reverse=True)
    top = rows[:10]
    others = rows[10:]

    lines: List[str] = ["Totals & Allocation:"]
    for row in top:
        share_base = max(
            abs(row.get("net_usd", Decimal("0"))),
            row.get("in_usd", Decimal("0")),
            row.get("out_usd", Decimal("0")),
        )
        pct = (share_base / base_total * 100) if base_total else Decimal("0")
        lines.append(
            " - {asset}: NET ${net_usd} | Realized ${realized} | Share {share}".format(
                asset=row.get("asset", "?"),
                net_usd=_fmt_decimal(row.get("net_usd", Decimal("0"))),
                realized=_fmt_decimal(row.get("realized_usd", Decimal("0"))),
                share=_fmt_pct(pct),
            )
        )

    if others:
        others_net = sum(row.get("net_usd", Decimal("0")) for row in others)
        others_realized = sum(row.get("realized_usd", Decimal("0")) for row in others)
        share_base = sum(
            max(
                abs(row.get("net_usd", Decimal("0"))),
                row.get("in_usd", Decimal("0")),
                row.get("out_usd", Decimal("0")),
            )
            for row in others
        )
        pct = (share_base / base_total * 100) if base_total else Decimal("0")
        lines.append(
            " - Others: NET ${net_usd} | Realized ${realized} | Share {share}".format(
                net_usd=_fmt_decimal(others_net),
                realized=_fmt_decimal(others_realized),
                share=_fmt_pct(pct),
            )
        )

    lines.append("")
    lines.append(
        "Totals — IN ${in_usd} / OUT ${out_usd} / NET ${net_usd} / Realized ${realized}".format(
            in_usd=_fmt_decimal(totals_row["in_usd"]),
            out_usd=_fmt_decimal(totals_row["out_usd"]),
            net_usd=_fmt_decimal(totals_row["net_usd"]),
            realized=_fmt_decimal(totals_row["realized_usd"]),
        )
    )
    return "\n".join(lines)


def handle_daily() -> str:
    snapshot = get_snapshot()
    wallet = os.getenv("WALLET_ADDRESS", "")
    return build_day_report_text(
        intraday=False,
        wallet=wallet,
        snapshot=snapshot,
        day=ymd(),
    )


def handle_weekly(days: int = 7) -> str:
    wallet = os.getenv("WALLET_ADDRESS", "")
    return build_weekly_report_text(days=days, wallet=wallet)


def _entries_for_asset(symbol: str) -> List[Dict[str, Any]]:
    symbol = symbol.upper()
    return [
        entry
        for entry in iter_all_entries()
        if str(entry.get("asset") or "").upper() == symbol
    ]


def handle_pnl(symbol: Optional[str] = None) -> str:
    if symbol:
        entries = _entries_for_asset(symbol)
        symbol = symbol.upper()
        if not entries:
            return f"No ledger entries for {symbol}."

        buys = [e for e in entries if str(e.get("side")).upper() == "IN"]
        sells = [e for e in entries if str(e.get("side")).upper() == "OUT"]
        realized = sum(Decimal(e.get("realized_usd", 0)) for e in entries)
        qty_net = sum(
            Decimal(e.get("qty", 0))
            * (1 if str(e.get("side")).upper() == "IN" else -1)
            for e in entries
        )
        spent = sum(Decimal(e.get("usd", 0)) for e in buys)
        received = sum(Decimal(e.get("usd", 0)) for e in sells)

        lines = [f"PnL for {symbol}:"]
        lines.append(f" - Buys: {len(buys)} totaling ${_fmt_decimal(spent)}")
        lines.append(f" - Sells: {len(sells)} totaling ${_fmt_decimal(received)}")
        lines.append(f" - Net qty: {_fmt_decimal(qty_net)}")
        lines.append(f" - Realized PnL: ${_fmt_decimal(realized)}")
        return "\n".join(lines)

    # No symbol: δείξε Top movers
    rows = aggregate_per_asset(iter_all_entries())
    if not rows:
        return "Ledger is empty."
    rows.sort(key=lambda row: abs(row.get("realized_usd", Decimal("0"))), reverse=True)
    top = rows[:5]

    lines = ["Top movers by realized PnL:"]
    for row in top:
        realized = row.get("realized_usd", Decimal("0"))
        direction = "+" if realized >= 0 else ""
        lines.append(
            f" - {row.get('asset', '?')}: {direction}${_fmt_decimal(realized)} "
            f"(net ${_fmt_decimal(row.get('net_usd', Decimal('0')))} )"
        )
    return "\n".join(lines)


def handle_tx(symbol: Optional[str] = None, day: Optional[str] = None) -> str:
    day = day or ymd()
    entries = read_ledger(day)

    if symbol:
        symbol = symbol.upper()
        entries = [
            entry
            for entry in entries
            if str(entry.get("asset") or "").upper() == symbol
        ]

    if not entries:
        target = f" for {symbol}" if symbol else ""
        return f"No transactions on {day}{target}."

    limit = 20
    lines: List[str] = [f"Transactions on {day}{(' for ' + symbol) if symbol else ''}:"]
    for entry in entries[:limit]:
        ts = entry.get("time")
        if ts:
            try:
                dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                ts_text = dt.strftime("%H:%M:%S")
            except Exception:
                ts_text = str(ts)
        else:
            ts_text = "--"

        lines.append(
            " - {ts} {side} {asset} {qty} @ ${usd}".format(
                ts=ts_text,
                side=str(entry.get("side") or "?").upper(),
                asset=str(entry.get("asset") or "?").upper(),
                qty=_fmt_decimal(entry.get("qty", Decimal("0"))),
                usd=_fmt_decimal(entry.get("usd", Decimal("0"))),
            )
        )

    if len(entries) > limit:
        lines.append(f"{len(entries) - limit} more ...")

    return "\n".join(lines)
