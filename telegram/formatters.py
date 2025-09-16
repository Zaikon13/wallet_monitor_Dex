import re
from decimal import Decimal

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
