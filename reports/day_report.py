from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from core.tz import ymd
from reports.aggregates import aggregate_per_asset, totals
from reports.ledger import read_ledger


def _fmt_decimal(value: Decimal) -> str:
    value = Decimal(value)
    if value == 0:
        return "0"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.6f}"


def _snapshot_summary(snapshot: Optional[Dict[str, Dict[str, Any]]]) -> tuple[int, Decimal]:
    if not snapshot:
        return 0, Decimal("0")
    total = Decimal("0")
    for info in snapshot.values():
        try:
            total += Decimal(str(info.get("usd", info.get("value_usd", "0")) or "0"))
        except Exception:
            continue
    return len(snapshot), total


def build_day_report_text(
    intraday: bool = False,
    wallet: Optional[str] = None,
    snapshot: Optional[Dict[str, Dict[str, Any]]] = None,
    day: Optional[str] = None,
) -> str:
    day = day or ymd()

    entries = read_ledger(day)
    rows = aggregate_per_asset(entries, wallet=wallet)
    totals_row = totals(rows)

    title = " Intraday Update" if intraday else f" Daily Report ({day})"
    lines = [title]

    # Optional holdings snapshot header
    assets_count, est_total = _snapshot_summary(snapshot)
    if assets_count:
        lines.append(f"Holdings snapshot: {assets_count} assets · est. ${_fmt_decimal(est_total)}")
        lines.append("")

    if not entries:
        lines.append("No transactions recorded.")
        return "\n".join(lines)

    lines.append("Per-asset activity:")
    for row in rows:
        asset = row.get("asset", "?")
        lines.append(
            " - {asset}: IN {in_qty} (${in_usd}) / OUT {out_qty} (${out_usd}) / NET ${net_usd} / Realized ${realized}".format(
                asset=asset,
                in_qty=_fmt_decimal(row.get("in_qty", Decimal("0"))),
                in_usd=_fmt_decimal(row.get("in_usd", Decimal("0"))),
                out_qty=_fmt_decimal(row.get("out_qty", Decimal("0"))),
                out_usd=_fmt_decimal(row.get("out_usd", Decimal("0"))),
                net_usd=_fmt_decimal(row.get("net_usd", Decimal("0"))),
                realized=_fmt_decimal(row.get("realized_usd", Decimal("0"))),
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
