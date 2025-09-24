from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from core.tz import now_gr, ymd
from reports.aggregates import aggregate_per_asset, totals
from reports.ledger import read_ledger


def _fmt(value: Decimal) -> str:
    value = Decimal(value)
    if value == 0:
        return "0"
    if abs(value) >= 1:
        return f"{value:,.2f}"
    return f"{value:.6f}"


def _collect_entries(days: int) -> List[Dict[str, object]]:
    end = now_gr()
    dates = [ymd(end - timedelta(days=offset)) for offset in range(days)]
    entries: List[Dict[str, object]] = []
    for day in dates[::-1]:
        entries.extend(read_ledger(day))
    return entries


def build_weekly_report_text(days: int = 7, wallet: Optional[str] = None) -> str:
    days = max(1, min(31, int(days or 7)))
    entries = _collect_entries(days)
    rows = aggregate_per_asset(entries, wallet=wallet)
    totals_row = totals(rows)

    end = now_gr()
    start = end - timedelta(days=days - 1)
    title = f"🧾 Period Summary ({start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')})"
    lines = [title, ""]

    if not rows:
        lines.append("No ledger entries in selected period.")
        return "\n".join(lines)

    lines.append("Per-asset deltas:")
    for row in rows:
        asset = row.get("asset", "?")
        lines.append(
            " - {asset}: IN ${in_usd} / OUT ${out_usd} / NET ${net_usd} / Realized ${realized}".format(
                asset=asset,
                in_usd=_fmt(row.get("in_usd", Decimal("0"))),
                out_usd=_fmt(row.get("out_usd", Decimal("0"))),
                net_usd=_fmt(row.get("net_usd", Decimal("0"))),
                realized=_fmt(row.get("realized_usd", Decimal("0"))),
            )
        )

    winners = sorted(rows, key=lambda r: r.get("realized_usd", Decimal("0")), reverse=True)
    losers = sorted(rows, key=lambda r: r.get("realized_usd", Decimal("0")))

    top_winners = [r for r in winners if r.get("realized_usd", Decimal("0")) > 0][:5]
    top_losers = [r for r in losers if r.get("realized_usd", Decimal("0")) < 0][:5]

    if top_winners:
        lines.append("")
        lines.append("Top winners:")
        for row in top_winners:
            lines.append(
                f"   • {row['asset']}: +${_fmt(row.get('realized_usd', Decimal('0')))}"
            )

    if top_losers:
        lines.append("")
        lines.append("Top losers:")
        for row in top_losers:
            lines.append(
                f"   • {row['asset']}: ${_fmt(row.get('realized_usd', Decimal('0')))}"
            )

    lines.append("")
    lines.append(
        "Totals — IN ${in_usd} / OUT ${out_usd} / NET ${net_usd} / Realized ${realized}".format(
            in_usd=_fmt(totals_row["in_usd"]),
            out_usd=_fmt(totals_row["out_usd"]),
            net_usd=_fmt(totals_row["net_usd"]),
            realized=_fmt(totals_row["realized_usd"]),
        )
    )

    return "\n".join(lines)
