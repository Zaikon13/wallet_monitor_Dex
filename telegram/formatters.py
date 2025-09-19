from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

def escape_md(text: str) -> str:
    """
    Escapes text for Telegram MarkdownV2
    """
    if not text:
        return ""
    return re.sub(r'([_*>\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))

def format_holdings(snapshot: dict) -> str:
    """
    Pretty-format holdings snapshot for Telegram
    """
    if not snapshot:
        return "❌ Empty snapshot"

    lines = ["\U0001F4B0 *Holdings Snapshot*", ""]

    for symbol, item in snapshot.items():
        amount = Decimal(item.get("amount", 0))
        price = Decimal(item.get("price", 0))
        value = amount * price

        line = f"`{symbol:<6}` {amount:>12,.4f} × ${price:.4f} = ${value:,.2f}"
        lines.append(line)

    total_usd = sum(Decimal(item.get("amount", 0)) * Decimal(item.get("price", 0)) for item in snapshot.values())
    lines.append("\n\n*Total:* $" + f"{total_usd:,.2f}")

    return escape_md("\n".join(lines))


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _format_quantity(value: Decimal) -> str:
    return format(value, "f")


def _format_usd(value: Decimal) -> str:
    absolute = abs(value)
    if absolute >= Decimal("1"):
        quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"${quantized:,.2f}"
    if absolute == 0:
        return "$0.00"
    return f"${format(value, 'f')}"


def format_per_asset_totals(
    period_label: str,
    rows: Iterable[Mapping[str, object]] | None,
) -> str:
    """Render aggregated per-asset totals for Telegram delivery."""

    label = (period_label or "Period").strip() or "Period"
    heading = f"\U0001F4C8 Totals per Asset — {label.title()}"
    lines = [heading, ""]

    has_rows = False
    for row in rows or []:
        has_rows = True
        asset = str(row.get("asset") or "?")
        tx_count = int(row.get("tx_count") or 0)
        in_qty = _to_decimal(row.get("in_qty"))
        out_qty = _to_decimal(row.get("out_qty"))
        net_qty = _to_decimal(row.get("net_qty"))
        in_usd = _to_decimal(row.get("in_usd"))
        out_usd = _to_decimal(row.get("out_usd"))
        net_usd = _to_decimal(row.get("net_usd"))
        realized = _to_decimal(row.get("realized_usd"))

        lines.extend(
            [
                f"{asset} — TXs: {tx_count}",
                f"  In: {_format_quantity(in_qty)} ({_format_usd(in_usd)})",
                f"  Out: {_format_quantity(out_qty)} ({_format_usd(out_usd)})",
                f"  Net: {_format_quantity(net_qty)} ({_format_usd(net_usd)})",
                f"  Realized PnL: {_format_usd(realized)}",
                "",
            ]
        )

    if not has_rows:
        lines.append("No activity recorded.")

    return "\n".join(lines).rstrip()
