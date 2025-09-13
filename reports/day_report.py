# -*- coding: utf-8 -*-
"""
Day report builder
- Timezone-safe timestamps
- Graceful empty breakdowns

build_day_report_text(date_str, entries, net_flow, realized_today_total, holdings_total, breakdown, unrealized, data_dir)
returns Markdown string for Telegram (MarkdownV2 escaping handled by sender)
"""
from datetime import datetime
from decimal import Decimal


def _format_amount(a):
    try:
        a = float(a)
    except Exception:
        return str(a)
    if abs(a) >= 1:
        return f"{a:,.2f}"
    if abs(a) >= 0.0001:
        return f"{a:.6f}"
    return f"{a:.8f}"


def _format_price(p):
    try:
        p = float(p)
    except Exception:
        return str(p)
    if p >= 1:
        return f"{p:,.6f}"
    if p >= 0.01:
        return f"{p:.6f}"
    if p >= 1e-6:
        return f"{p:.8f}"
    return f"{p:.10f}"


def _ts_of(e):
    t = e.get("time") or ""
    try:
        # accept both naive and tz-aware ISO strings
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    except Exception:
        return datetime.utcnow()  # fallback


def build_day_report_text(*, date_str, entries, net_flow, realized_today_total, holdings_total, breakdown, unrealized, data_dir):
    sorted_entries = sorted(entries or [], key=_ts_of)

    lines = [f"*📅 Ημέρα:* {date_str}"]
    lines.append("")

    if sorted_entries:
        lines.append("*Συναλλαγές σήμερα:*")
        for e in sorted_entries[-20:]:
            tok = e.get("token") or "?"
            amt = e.get("amount") or 0
            px  = e.get("price_usd") or 0
            usd = e.get("usd_value") or 0
            rp  = e.get("realized_pnl") or 0
            tm  = (e.get("time", "")[11:19]) or "--:--:--"
            lines.append(f"• {tm} {tok} {amt:+.6f} @ ${_format_price(px)} → ${_format_amount(usd)} (real ${_format_amount(rp)})")
        lines.append("")
    else:
        lines.append("*Συναλλαγές σήμερα:* (καμία)")
        lines.append("")

    lines.append(f"*Net flow σήμερα:* ${_format_amount(net_flow)}")
    lines.append(f"*Realized σήμερα:* ${_format_amount(realized_today_total)}")
    if abs(float(unrealized or 0)) > 1e-9:
        lines.append(f"*Unrealized τώρα:* ${_format_amount(unrealized)}")
    lines.append("")

    lines.append(f"*Holdings (MTM) τώρα:* ${_format_amount(holdings_total)}")
    if breakdown:
        for b in (breakdown or [])[:15]:
            tok = b.get("token") or "?"
            amt = b.get("amount") or 0
            px  = b.get("price_usd") or 0
            val = b.get("usd_value") or 0
            lines.append(f"• {tok}: {amt:.6f} @ ${_format_price(px)} = ${_format_amount(val)}")
        if len(breakdown) > 15:
            lines.append(f"… και άλλα {len(breakdown)-15}")
    else:
        lines.append("(No holdings to display)")

    return "\n".join(lines)


__all__ = ["build_day_report_text"]
