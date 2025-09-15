#reports/day_report.py
# -*- coding: utf-8 -*-
"""
Day report builder (Markdown string for Telegram)
- Timezone-safe timestamp parsing (accepts naive or Z/offset ISO)
- Graceful when there are no entries or holdings

build_day_report_text(
    *,
    date_str: str,
    entries: list,
    net_flow: float,
    realized_today_total: float,
    holdings_total: float,
    breakdown: list,
    unrealized: float,
    data_dir: str,
) -> str
"""
from __future__ import annotations
from datetime import datetime, timezone


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


def _parse_ts(t: str):
    if not t:
        return None
    try:
        # Accept "YYYY-mm-dd HH:MM:SS" or ISO with/without Z
        if "T" in t:
            t = t.replace("Z", "+00:00")
            return datetime.fromisoformat(t)
        return datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def build_day_report_text(*, date_str, entries, net_flow, realized_today_total, holdings_total, breakdown, unrealized, data_dir):
    # sort by timestamp if available
    def _key(e):
        dt = _parse_ts(e.get("time", ""))
        return dt or datetime.min.replace(tzinfo=timezone.utc)

    sorted_entries = sorted(entries or [], key=_key)

    lines = [f"*📅 Ημέρα:* {date_str}", ""]

    if sorted_entries:
        lines.append("*Συναλλαγές σήμερα:*")
        # Keep last 20 for brevity
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
