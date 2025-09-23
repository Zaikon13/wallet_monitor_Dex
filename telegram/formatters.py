import re
from decimal import Decimal

# --- MarkdownV2 escaping ---
def escape_md(text: str) -> str:
    if not text:
        return ""
    # Escape Telegram MarkdownV2 specials
    return re.sub(r'([_*>\[\]()~`>#+\-=|{}.!])', r'\\\1', str(text))

# --- Holdings snapshot ---
def _dec(x) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")

def format_holdings(snapshot: dict) -> str:
    """
    Pretty-format holdings snapshot for Telegram.
    Accepts either:
      {"SYMBOL": {"amount": ..., "price": ...}}
    or
      {"SYMBOL": {"qty": ..., "price_usd": ...}}
    """
    if not snapshot:
        return "❌ Empty snapshot"

    lines = ["\U0001F4B0 *Holdings Snapshot*", ""]

    total_usd = Decimal("0")
    for symbol, item in snapshot.items():
        amount = _dec(item.get("amount", item.get("qty", 0)))
        price  = _dec(item.get("price", item.get("price_usd", 0)))
        value  = amount * price
        total_usd += value
        # Keep code block look for symbol, align numbers
        line = f"`{symbol:<8}` {amount:>12,.4f} × ${price:,.6f} = ${value:,.2f}"
        lines.append(line)

    lines.append("")
    lines.append(f"*Total:* ${total_usd:,.2f}")

    return escape_md("\n".join(lines))

# --- Per-asset totals (for tests) ---
def _period_title(p) -> str:
    if isinstance(p, str) and p.lower() == "today":
        return "Today"
    return str(p).title()

def format_per_asset_totals(period: str, rows: list[dict]) -> str:
    """
    Formats the output of reports.aggregates.aggregate_per_asset for Telegram.
    Includes TXs count (used in tests).
    """
    title = f"Totals per Asset — {_period_title(period)}"
    out = [f"*{title}*", ""]

    for r in rows or []:
        asset = r.get("asset", "?")
        in_qty  = _dec(r.get("in_qty"))
        out_qty = _dec(r.get("out_qty"))
        net_qty = _dec(r.get("net_qty"))
        in_usd  = _dec(r.get("in_usd"))
        out_usd = _dec(r.get("out_usd"))
        net_usd = _dec(r.get("net_usd"))
        txs     = int(r.get("tx_count", 0))

        line = (
            f"`{asset:<6}` "
            f"Q: {in_qty} in / {out_qty} out → {net_qty} net | "
            f"$: {in_usd} in / {out_usd} out → {net_usd} net | "
            f"TXs: {txs}"
        )
        out.append(line)

    return escape_md("\n".join(out))
