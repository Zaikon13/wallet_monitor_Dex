from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List

__all__ = [
    "escape_md",
    "escape_md_v2",
    "chunk",
    "format_holdings",
    "format_per_asset_totals",
]


# --- Markdown escaping helpers ---
_MD_V1_ESCAPE_RE = re.compile(r'([_*`\[\]()~>#+\-=|{}.!])')
_MD_V2_ESCAPE_RE = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')


def _ensure_text(value: object) -> str:
    return "" if value is None else str(value)


def escape_md(text: str) -> str:
    """Escape Markdown v1 special characters for Telegram bots."""

    raw = _ensure_text(text)
    if not raw:
        return ""
    return _MD_V1_ESCAPE_RE.sub(r'\\\1', raw)


def escape_md_v2(text: str) -> str:
    """Escape MarkdownV2 special characters according to Telegram docs."""

    raw = _ensure_text(text)
    if not raw:
        return ""
    return _MD_V2_ESCAPE_RE.sub(r'\\\1', raw)


def chunk(text: str, size: int = 3800) -> Iterable[str]:
    """Yield chunks of ``text`` limited to ``size`` characters (>=1)."""

    raw = _ensure_text(text)
    if not raw:
        return []
    safe_size = max(1, int(size))
    return [raw[idx : idx + safe_size] for idx in range(0, len(raw), safe_size)]


# --- Helpers ---
def _dec(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def format_holdings(snapshot: Dict[str, Dict[str, object]]) -> str:
    if not snapshot:
        return "Holdings snapshot is empty."

    lines: List[str] = ["Holdings snapshot:"]
    total_usd = Decimal("0")
    for symbol, data in sorted(snapshot.items()):
        qty = _dec(data.get("amount", data.get("qty", 0)))
        price = _dec(data.get("price", data.get("price_usd", 0)))
        value = qty * price
        total_usd += value
        lines.append(
            f" - {symbol.upper():<8} {qty:>12,.4f} × ${price:,.6f} = ${value:,.2f}"
        )

    lines.append("")
    lines.append(f"Total ≈ ${total_usd:,.2f}")
    return "\n".join(lines)


def format_per_asset_totals(period: str, rows: List[dict]) -> str:
    title = f"Totals per Asset — {str(period).title()}"
    lines = [title]
    for row in rows or []:
        asset = row.get("asset", "?")
        in_qty = _dec(row.get("in_qty"))
        out_qty = _dec(row.get("out_qty"))
        net_qty = _dec(row.get("net_qty"))
        in_usd = _dec(row.get("in_usd"))
        out_usd = _dec(row.get("out_usd"))
        net_usd = _dec(row.get("net_usd"))
        txs = int(row.get("tx_count", 0))
        lines.append(
            " - {asset:<6} Q: {in_qty} in / {out_qty} out → {net_qty} net | $: {in_usd} in / {out_usd} out → {net_usd} net | TXs: {txs}".format(
                asset=asset,
                in_qty=in_qty,
                out_qty=out_qty,
                net_qty=net_qty,
                in_usd=in_usd,
                out_usd=out_usd,
                net_usd=net_usd,
                txs=txs,
            )
        )
    return "\n".join(lines)

# --- APPEND: Intraday trades & PnL formatters ---

def format_trades_table(trades) -> str:
    """
    trades: List[reports.trades.Trade]
    """
    if not trades:
        return "Σήμερα δεν βρέθηκαν συναλλαγές."
    lines = ["Συναλλαγές (σήμερα):", "ts | symbol | side | qty @ price | fee | tx"]
    for t in trades:
        ts_s = t.ts.strftime("%H:%M:%S")
        tx_s = (t.tx[:8] + "…") if t.tx else "-"
        lines.append(f"{ts_s} | {t.symbol} | {t.side} | {t.qty:.8f} @ {t.price:.6f} | {t.fee:.6f} | {tx_s}")
    return "\n".join(lines)

def format_pnl_today(summary) -> str:
    """
    summary: reports.trades.RealizedSummary
    """
    if not summary.fills:
        return "Σήμερα δεν υπάρχει realized PnL (καμία πώληση ή κλείσιμο θέσης)."
    start = summary.window_start.strftime("%Y-%m-%d")
    lines = [f"Realized PnL (today {start}):"]
    for sym, d in sorted(summary.per_symbol.items()):
        realized = d.get("realized", 0.0)
        fees = d.get("fees", 0.0)
        qty = d.get("qty_sold", 0.0)
        net = realized - fees
        lines.append(f"- {sym}: realized={realized:.2f}, fees={fees:.2f}, qty_sold={qty:.8f}, net={net:.2f}")
    lines.append(f"\nTOTAL: realized={summary.total_realized:.2f}, fees={summary.total_fees:.2f}, net={summary.total_realized - summary.total_fees:.2f}")
    return "\n".join(lines)

