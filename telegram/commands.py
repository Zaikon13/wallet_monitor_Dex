from __future__ import annotations
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
from core.holdings_adapters import build_holdings_snapshot
try:
    from telegram.formatters import format_holdings as _format_holdings  # preferred
except Exception:
    _format_holdings = None


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
    """
    Returns a text summary of current holdings using live adapters.
    Keeps CRO distinct from tCRO and computes merged unrealized PnL.
    """
    try:
        snap = build_holdings_snapshot(base_ccy="USD")
    except Exception as e:
        return f"⚠️ Failed to build holdings snapshot: {e}"
    # Prefer repo formatter if available
    if _format_holdings:
        try:
            return _format_holdings(snap, limit=limit)
        except Exception:
            pass
    # Fallback minimal text formatter
    totals = snap.get("totals", {})
    assets = snap.get("assets", [])[: max(1, limit)]
    lines = []
    lines.append("📊 Holdings")
    for a in assets:
        lines.append(
            f"{a.get('symbol','?')}: amt={a.get('amount','0')}  "
            f"px=${a.get('price_usd','0')}  "
            f"val=${a.get('value_usd','0')}  "
            f"Δ=${a.get('u_pnl_usd','0')} ({a.get('u_pnl_pct','0')}%)"
        )
    lines.append(
        f"— Totals: val=${totals.get('value_usd','0')}  "
        f"cost=${totals.get('cost_usd','0')}  "
        f"Δ=${totals.get('u_pnl_usd','0')} ({totals.get('u_pnl_pct','0')}%)"
    )
    return "\n".join(lines)


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

        summary: Dict[str, Decimal] = {
            "in": Decimal("0"),
            "out": Decimal("0"),
            "net": Decimal("0"),
            "realized": Decimal("0"),
        }
        for entry in entries:
            summary["in"] += _to_decimal(entry.get("in_usd"))
            summary["out"] += _to_decimal(entry.get("out_usd"))
            summary["net"] += _to_decimal(entry.get("net_usd"))
            summary["realized"] += _to_decimal(entry.get("realized_usd"))

        return (
            f"PnL {symbol} — IN {_fmt_money(summary['in'])} / OUT {_fmt_money(summary['out'])}"
            f" / NET {_fmt_money(summary['net'])} / Realized {_fmt_money(summary['realized'])}"
        )

    # If no symbol provided, default to totals over all assets
    try:
        ledger = read_ledger()
    except Exception:
        return "PnL unavailable."

    totals: Dict[str, Decimal] = {
        "in": Decimal("0"),
        "out": Decimal("0"),
        "net": Decimal("0"),
        "realized": Decimal("0"),
    }
    for entry in ledger:
        totals["in"] += _to_decimal(entry.get("in_usd"))
        totals["out"] += _to_decimal(entry.get("out_usd"))
        totals["net"] += _to_decimal(entry.get("net_usd"))
        totals["realized"] += _to_decimal(entry.get("realized_usd"))

    return (
        "PnL — IN {in_usd} / OUT {out_usd} / NET {net_usd} / Realized {realized}".format(
            in_usd=_fmt_money(totals["in"]),
            out_usd=_fmt_money(totals["out"]),
            net_usd=_fmt_money(totals["net"]),
            realized=_fmt_money(totals["realized"]),
        )
    )


def daily_report_for_date(target_date: Optional[str] = None) -> str:
    if not target_date:
        target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        return build_day_report_text(target_date=target_date)
    except Exception:
        return f"Daily report unavailable for {target_date}."


def weekly_report_for_date(target_date: Optional[str] = None, days: int = 7) -> str:
    if not target_date:
        target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        return build_weekly_report_text(target_date=target_date, days=days)
    except Exception:
        return f"Weekly report unavailable for {target_date}."


def ledger_entries(symbol: Optional[str] = None, limit: int = 10) -> str:
    try:
        entries = list(iter_all_entries())
    except Exception:
        entries = []
    if not entries:
        return "No ledger entries available."

    if symbol:
        symbol = symbol.upper()
        entries = [
            entry
            for entry in entries
            if str(entry.get("asset") or "").upper() == symbol
        ]
        if not entries:
            return f"No ledger entries for {symbol}."

    entries = entries[:limit]
    lines = [f"Last {len(entries)} ledger entries:"]
    for entry in entries:
        date = entry.get("date") or ymd(entry.get("timestamp"))
        asset = entry.get("asset", "?")
        in_usd = _fmt_money(_to_decimal(entry.get("in_usd")))
        out_usd = _fmt_money(_to_decimal(entry.get("out_usd")))
        net_usd = _fmt_money(_to_decimal(entry.get("net_usd")))
        realized_usd = _fmt_money(_to_decimal(entry.get("realized_usd")))
        lines.append(
            f" - {date} {asset}: IN {in_usd} / OUT {out_usd} / NET {net_usd} / Realized {realized_usd}"
        )
    return "\n".join(lines)
