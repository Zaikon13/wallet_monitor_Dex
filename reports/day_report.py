from __future__ import annotations

"""Daily Markdown report builder with resilient fallbacks."""

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional

from core.tz import ymd
from reports.aggregates import aggregate_per_asset, totals
from reports.ledger import read_ledger


def _to_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _fmt_money(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    try:
        if value == 0:
            return "$0.00"
        if abs(value) >= 1:
            return f"${value:,.2f}"
        return f"${value:.6f}"
    except Exception:
        return "n/a"


def _fmt_qty(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    try:
        if value == 0:
            return "0"
        if abs(value) >= 1:
            return f"{value:,.4f}"
        return f"{value:.6f}"
    except Exception:
        return "n/a"


def _safe_sum(values: Iterable[Optional[Decimal]]) -> Optional[Decimal]:
    acc: Decimal = Decimal("0")
    has_value = False
    for value in values:
        if value is None:
            continue
        acc += value
        has_value = True
    return acc if has_value else None


def _top_movers(rows: Iterable[Dict[str, Any]], limit: int = 5) -> List[str]:
    movers: List[str] = []
    try:
        sorted_rows = sorted(
            rows,
            key=lambda row: (
                abs(_to_decimal(row.get("net_usd")) or Decimal("0")),
                abs(_to_decimal(row.get("realized_usd")) or Decimal("0")),
            ),
            reverse=True,
        )
    except Exception:
        sorted_rows = []

    for row in sorted_rows[:limit]:
        asset = str(row.get("asset") or "?").upper()
        net_usd = _fmt_money(_to_decimal(row.get("net_usd")))
        realized = _fmt_money(_to_decimal(row.get("realized_usd")))
        movers.append(f" - {asset}: NET {net_usd} · Realized {realized}")

    if not movers:
        movers.append(" - n/a")
    return movers


def _build_totals_line(totals_row: Dict[str, Any]) -> str:
    in_usd = _fmt_money(_to_decimal(totals_row.get("in_usd")))
    out_usd = _fmt_money(_to_decimal(totals_row.get("out_usd")))
    net_usd = _fmt_money(_to_decimal(totals_row.get("net_usd")))
    realized = _fmt_money(_to_decimal(totals_row.get("realized_usd")))
    return f"Totals — IN {in_usd} / OUT {out_usd} / NET {net_usd} / Realized {realized}"


def build_day_report_text() -> str:
    """Return a Markdown-safe day summary string resilient to missing data."""

    try:
        day = ymd()
    except Exception:
        day = "n/a"

    try:
        entries = read_ledger(day) or []
    except Exception:
        entries = []

    try:
        rows = aggregate_per_asset(entries)
    except Exception:
        rows = []

    try:
        totals_row = totals(rows)
    except Exception:
        totals_row = {
            "in_usd": Decimal("0"),
            "out_usd": Decimal("0"),
            "net_usd": Decimal("0"),
            "realized_usd": Decimal("0"),
        }

    snapshot: Dict[str, Dict[str, Any]] = {}
    try:
        from core.holdings import holdings_snapshot  # local import to avoid cycles

        snapshot = holdings_snapshot()
    except Exception:
        snapshot = {}

    cro_balance = _to_decimal((snapshot.get("CRO") or {}).get("qty"))
    tcro_balance = _to_decimal((snapshot.get("tCRO") or {}).get("qty"))
    wallet_usd = _safe_sum(
        _to_decimal((info or {}).get("usd") or (info or {}).get("value_usd"))
        for info in snapshot.values()
    )
    realized_total = _to_decimal(totals_row.get("realized_usd"))
    unrealized_total = _safe_sum(
        _to_decimal((info or {}).get("unrealized_usd"))
        or _to_decimal((info or {}).get("pnl_unrealized"))
        or _to_decimal((info or {}).get("unrealized"))
        for info in snapshot.values()
    )

    lines: List[str] = [f"Daily Report ({day})"]
    lines.append(
        "Wallet totals: USD {usd} | CRO {cro} (+ tCRO {tcro})".format(
            usd=_fmt_money(wallet_usd),
            cro=_fmt_qty(cro_balance),
            tcro=_fmt_qty(tcro_balance),
        )
    )
    lines.append(f"Realized PnL: {_fmt_money(realized_total)}")
    lines.append(f"Unrealized PnL: {_fmt_money(unrealized_total)}")
    lines.append("")
    lines.append("Top movers:")
    lines.extend(_top_movers(rows))
    lines.append("")
    lines.append(_build_totals_line(totals_row))

    if not entries:
        lines.append("Note: ledger entries unavailable or empty for this day.")

    return "\n".join(lines)
