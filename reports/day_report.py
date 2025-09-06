# reports/day_report.py
# -*- coding: utf-8 -*-

from datetime import datetime
import os


# ---- μικρο-helpers τοπικά (ώστε να μην υπάρχει κυκλικό import) ----
def _format_amount(a):
    try:
        a = float(a)
    except Exception:
        return str(a)
    if abs(a) >= 1:
        return f"{a:,.4f}"
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


def _nonzero(v, eps=1e-12):
    try:
        return abs(float(v)) > eps
    except Exception:
        return False


def month_prefix_from(date_str: str) -> str:
    # date_str: "YYYY-MM-DD"
    return date_str[:7]


def sum_month_net_flows_and_realized(data_dir: str, month_prefix: str) -> tuple[float, float]:
    """Συνοψίζει Month Net Flow και Month Realized PnL με βάση τα αρχεία transactions_YYYY-MM-DD.json"""
    total_flow = 0.0
    total_real = 0.0
    try:
        for fn in os.listdir(data_dir):
            if fn.startswith("transactions_") and fn.endswith(".json") and month_prefix in fn:
                # πολύ μικρό και γρήγορο parse χωρίς dependencies
                path = os.path.join(data_dir, fn)
                try:
                    import json
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        total_flow += float(data.get("net_usd_flow", 0.0))
                        total_real += float(data.get("realized_pnl", 0.0))
                except Exception:
                    pass
    except Exception:
        pass
    return total_flow, total_real


def build_day_report_text(
    *,
    date_str: str,
    entries: list[dict],
    net_flow: float,
    realized_today_total: float,
    holdings_total: float,
    breakdown: list[dict],
    unrealized: float,
    data_dir: str = "/app/data",
) -> str:
    """
    Συνθέτει το Daily Report ΜΟΝΟ από έτοιμα inputs (για να αποφύγουμε κυκλικά imports).
    - Τα holdings (total/breakdown/unrealized) τα έχει ήδη υπολογίσει ο caller (main.py)
    - Το month summary υπολογίζεται εδώ (από τα αρχεία)
    """
    lines = [f"*📒 Daily Report* ({date_str})"]

    if not entries:
        lines.append("_No transactions today._")
    else:
        def _ts_of(e):
            try:
                return datetime.strptime(e.get("time", "")[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                return datetime.now()

        sorted_entries = sorted(entries, key=_ts_of)

        lines.append("*Transactions:*")
        MAX_TX_LINES = 60
        cut = max(0, len(sorted_entries) - MAX_TX_LINES)
        shown = sorted_entries[-MAX_TX_LINES:] if cut > 0 else sorted_entries

        for e in shown:
            tok = e.get("token") or "?"
            amt = float(e.get("amount") or 0)
            usd = float(e.get("usd_value") or 0)
            tm = (e.get("time", "")[-8:]) or ""
            direction = "IN" if amt > 0 else "OUT"
            unit_price = float(e.get("price_usd") or 0.0)
            rp = float(e.get("realized_pnl", 0.0) or 0.0)
            pnl_line = f"  PnL: ${_format_amount(rp)}" if _nonzero(rp) else ""
            lines.append(
                f"• {tm} — {direction} {tok} {_format_amount(amt)}  @ ${_format_price(unit_price)}  "
                f"(${_format_amount(usd)}){pnl_line}"
            )

        if cut > 0:
            lines.append(f"_…and {cut} earlier txs._")

    # Holdings
    lines.append(f"\n*Net USD flow today:* ${_format_amount(net_flow)}")
    lines.append(f"*Realized PnL today:* ${_format_amount(realized_today_total)}")
    lines.append(f"*Holdings (MTM) now:* ${_format_amount(holdings_total)}")

    if breakdown:
        for b in breakdown[:15]:
            tok = b.get("token", "?")
            amt = float(b.get("amount") or 0.0)
            pr = float(b.get("price_usd") or 0.0)
            val = float(b.get("usd_value") or 0.0)
            lines.append(f"  – {tok}: {_format_amount(amt)} @ ${_format_price(pr)} = ${_format_amount(val)}")
        if len(breakdown) > 15:
            lines.append(f"  …and {len(breakdown) - 15} more.")

    if _nonzero(unrealized):
        lines.append(f"*Unrealized PnL (open positions):* ${_format_amount(unrealized)}")

    # Month totals
    mp = month_prefix_from(date_str)
    month_flow, month_real = sum_month_net_flows_and_realized(data_dir, mp)
    lines.append(f"\n*Month Net Flow:* ${_format_amount(month_flow)}")
    lines.append(f"*Month Realized PnL:* ${_format_amount(month_real)}")

    return "\n".join(lines)
