from __future__ import annotations

"""Telegram command handlers that return plain text responses."""

import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from core.holdings import holdings_snapshot, holdings_text
from core.runtime_state import get_state
from core.tz import ymd
from reports.aggregates import aggregate_per_asset, totals as totals_aggregated
from reports.day_report import build_day_report_text
from reports.ledger import iter_all_entries, read_ledger
from reports.weekly import build_weekly_report_text


# ---------- helpers ----------


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _fmt_money(value: Decimal) -> str:
    try:
        if value == 0:
            return "$0.00"
        if abs(value) >= 1:
            return f"${value:,.2f}"
        return f"${value:.6f}"
    except Exception:
        return "$0.00"


def _fmt_qty(value: Decimal) -> str:
    try:
        if value == 0:
            return "0"
        if abs(value) >= 1:
            return f"{value:,.4f}"
        return f"{value:.6f}"
    except Exception:
        return "0"


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


def _entries_for_asset(symbol: str) -> List[Dict[str, Any]]:
    symbol = symbol.upper()
    return [
        entry
        for entry in iter_all_entries() or []
        if str(entry.get("asset") or "").upper() == symbol
    ]


def _asset_usd(snapshot: Dict[str, Dict[str, Any]], symbol: str) -> Decimal:
    info = snapshot.get(symbol) or {}
    return _to_decimal(info.get("usd") or info.get("value_usd"))


def _ordered_assets(snapshot: Dict[str, Dict[str, Any]]) -> List[Tuple[str, Decimal]]:
    symbols = list(snapshot.keys())
    ordered: List[Tuple[str, Decimal]] = []
    for special in ("CRO", "tCRO"):
        if special in symbols:
            ordered.append((special, _asset_usd(snapshot, special)))
            symbols.remove(special)
    symbols.sort(key=lambda sym: (_asset_usd(snapshot, sym), sym), reverse=True)
    for sym in symbols:
        ordered.append((sym, _asset_usd(snapshot, sym)))
    return ordered


# ---------- high-level command strings ----------


def holdings(limit: int = 20) -> str:
    try:
        snapshot = holdings_snapshot()
    except Exception:
        snapshot = {}
    if not snapshot:
        return "No holdings data."

    ordered = _ordered_assets(snapshot)
    limited_symbols = [symbol for symbol, _ in ordered[:limit]]
    filtered_snapshot = {symbol: snapshot.get(symbol, {}) for symbol in limited_symbols}
    return holdings_text(filtered_snapshot)


def totals() -> str:
    try:
        rows = aggregate_per_asset(iter_all_entries())
        summary = totals_aggregated(rows)
    except Exception:
        return "Totals unavailable."
    return (
        "Totals — IN {in_usd} / OUT {out_usd} / NET {net_usd} / Realized {realized}".format(
            in_usd=_fmt_money(_to_decimal(summary.get("in_usd"))),
            out_usd=_fmt_money(_to_decimal(summary.get("out_usd"))),
            net_usd=_fmt_money(_to_decimal(summary.get("net_usd"))),
            realized=_fmt_money(_to_decimal(summary.get("realized_usd"))),
        )
    )


def daily() -> str:
    try:
        return build_day_report_text()
    except Exception:
        return "Daily report unavailable."


def show(limit: int = 5) -> str:
    try:
        snapshot = holdings_snapshot()
    except Exception:
        snapshot = {}
    if not snapshot:
        return "No holdings data."

    ordered = _ordered_assets(snapshot)
    top_n = ordered[:limit]
    lines = [f"Top {len(top_n)} holdings:"]
    for symbol, usd in top_n:
        info = snapshot.get(symbol) or {}
        qty = _fmt_qty(_to_decimal(info.get("qty") or info.get("amount")))
        lines.append(f" - {symbol:<6} {qty} ≈ {_fmt_money(usd)}")
    return "\n".join(lines)


def status() -> str:
    return "Cronos DeFi Sentinel online"


def diag() -> str:
    state = get_state()
    lines: List[str] = ["Diagnostics:"]
    rpc_ok_ts = state.get("last_rpc_ok_ts")
    rpc_error = state.get("last_rpc_error")
    if rpc_ok_ts:
        lines.append(f"RPC reachable: yes ({_format_age(rpc_ok_ts)})")
    elif rpc_error:
        lines.append(f"RPC reachable: no (last error: {rpc_error})")
    else:
        lines.append("RPC reachable: unknown")

    wallet = os.getenv("WALLET_ADDRESS", "")
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


def weekly(days: int = 7) -> str:
    wallet = os.getenv("WALLET_ADDRESS", "")
    try:
        return build_weekly_report_text(days=days, wallet=wallet)
    except Exception:
        return "Weekly report unavailable."


def pnl(symbol: Optional[str] = None) -> str:
    if symbol:
        entries = _entries_for_asset(symbol)
        symbol = symbol.upper()
        if not entries:
            return f"No ledger entries for {symbol}."

        buys = [e for e in entries if str(e.get("side")).upper() == "IN"]
        sells = [e for e in entries if str(e.get("side")).upper() == "OUT"]
        realized = sum(_to_decimal(e.get("realized_usd")) for e in entries)
        qty_net = sum(
            _to_decimal(e.get("qty"))
            * (1 if str(e.get("side")).upper() == "IN" else -1)
            for e in entries
        )
        spent = sum(_to_decimal(e.get("usd")) for e in buys)
        received = sum(_to_decimal(e.get("usd")) for e in sells)

        lines = [f"PnL for {symbol}:"]
        lines.append(f" - Buys: {len(buys)} totaling {_fmt_money(spent)}")
        lines.append(f" - Sells: {len(sells)} totaling {_fmt_money(received)}")
        lines.append(f" - Net qty: {_fmt_qty(qty_net)}")
        lines.append(f" - Realized PnL: {_fmt_money(realized)}")
        return "\n".join(lines)

    rows = aggregate_per_asset(iter_all_entries())
    if not rows:
        return "Ledger is empty."
    rows.sort(key=lambda row: abs(_to_decimal(row.get("realized_usd"))), reverse=True)
    top = rows[:5]

    lines = ["Top movers by realized PnL:"]
    for row in top:
        realized = _to_decimal(row.get("realized_usd"))
        direction = "+" if realized >= 0 else ""
        lines.append(
            f" - {row.get('asset', '?')}: {direction}{_fmt_money(realized)} "
            f"(net {_fmt_money(_to_decimal(row.get('net_usd')))} )"
        )
    return "\n".join(lines)


def tx(symbol: Optional[str] = None, day: Optional[str] = None) -> str:
    target_day = day or ymd()
    try:
        entries = read_ledger(target_day) or []
    except Exception:
        entries = []

    if symbol:
        symbol = symbol.upper()
        entries = [
            entry
            for entry in entries
            if str(entry.get("asset") or "").upper() == symbol
        ]

    if not entries:
        target = f" for {symbol}" if symbol else ""
        return f"No transactions on {target_day}{target}."

    limit = 20
    lines: List[str] = [f"Transactions on {target_day}{(' for ' + symbol) if symbol else ''}:"]
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
                qty=_fmt_qty(_to_decimal(entry.get("qty"))),
                usd=_fmt_money(_to_decimal(entry.get("usd"))),
            )
        )

    if len(entries) > limit:
        lines.append(f"{len(entries) - limit} more ...")

    return "\n".join(lines)


# ---------- compatibility wrappers ----------


def handle_holdings(limit: int = 20) -> str:
    return holdings(limit=limit)


def handle_totals() -> str:
    return totals()


def handle_daily() -> str:
    return daily()


def handle_show(limit: int = 5) -> str:
    return show(limit=limit)


def handle_status() -> str:
    return status()


def handle_diag() -> str:
    return diag()


def handle_weekly(days: int = 7) -> str:
    return weekly(days=days)


def handle_pnl(symbol: Optional[str] = None) -> str:
    return pnl(symbol=symbol)


def handle_tx(symbol: Optional[str] = None, day: Optional[str] = None) -> str:
    return tx(symbol=symbol, day=day)
