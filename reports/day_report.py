# -*- coding: utf-8 -*-
from datetime import datetime
from decimal import Decimal

def _format_amount(a):
    try: a=float(a)
    except: return str(a)
    if abs(a)>=1: return f"{a:,.4f}"
    if abs(a)>=0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

def _format_price(p):
    try: p=float(p)
    except: return str(p)
    if p>=1: return f"{p:,.6f}"
    if p>=0.01: return f"{p:.6f}"
    if p>=1e-6: return f"{p:.8f}"
    return f"{p:.10f}"

def _ts_of(e, tz):
    t = e.get("time") or ""
    try:
        # Expect "YYYY-MM-DD HH:MM:SS"
        dt = datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
        return tz.localize(dt) if hasattr(tz, "localize") else dt.replace(tzinfo=tz)
    except Exception:
        from datetime import datetime as _dt
        return _dt.now(tz)

def build_day_report_text(*, date_str, entries, net_flow, realized_today_total, holdings_total, breakdown, unrealized, data_dir, tz):
    lines = [f"*📅 Report {date_str}*"]
    # Entries (latest first)
    try:
        sorted_entries = sorted(entries or [], key=lambda e: e.get("time",""), reverse=True)
    except Exception:
        sorted_entries = entries or []

    lines.append(f"*Activity:* {len(sorted_entries)} tx")
    for e in sorted_entries[:20]:
        tok = e.get("token") or "?"
        amt = e.get("amount") or 0
        pr  = e.get("price_usd") or 0
        usd = e.get("usd_value") or 0
        lines.append(f"• {tok} {amt:+.6f} @ ${_format_price(pr)} = ${_format_amount(usd)}")

    lines.append("")
    lines.append(f"*Holdings (MTM) now:* ${_format_amount(holdings_total)}")
    if breakdown:
        for b in breakdown[:15]:
            lines.append(f"• {b['token']}: {_format_amount(b['amount'])} @ ${_format_price(b.get('price_usd',0))} = ${_format_amount(b.get('usd_value',0))}")
    else:
        lines.append("• No holdings")

    lines.append("")
    lines.append(f"*Realized today:* ${_format_amount(realized_today_total)}")
    lines.append(f"*Net flow today:* ${_format_amount(net_flow)}")
    if abs(float(unrealized or 0)) > 1e-9:
        lines.append(f"*Unrealized open:* ${_format_amount(unrealized)}")

    return "\n".join(lines)
