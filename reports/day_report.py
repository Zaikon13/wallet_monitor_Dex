# reports/day_report.py
def build_day_report_text(
    date_str: str,
    entries: list,
    net_flow: float,
    realized_today_total: float,
    holdings_total: float,
    breakdown: list,
    unrealized: float,
    data_dir: str,
) -> str:
    lines = [f"*📒 Daily Report* ({date_str})"]
    if not entries:
        lines.append("_No transactions today._")
    else:
        lines.append("*Transactions:*")
        for e in entries[-20:]:
            tok = e.get("token") or "?"
            amt = e.get("amount") or 0
            usd = e.get("usd_value") or 0
            tm = (e.get("time", "")[-8:]) or ""
            direction = "IN" if float(amt) > 0 else "OUT"
            unit_price = e.get("price_usd") or 0.0
            pnl_line = ""
            rp = float(e.get("realized_pnl", 0.0) or 0.0)
            if abs(rp) > 1e-9:
                pnl_line = f"  PnL: ${rp:.6f}"
            lines.append(
                f"• {tm} — {direction} {tok} {amt:.6f} "
                f"@ ${unit_price:.6f} (${usd:.2f}){pnl_line}"
            )
    lines.append(f"\n*Net USD flow today:* ${net_flow:.2f}")
    lines.append(f"*Realized PnL today:* ${realized_today_total:.2f}")
    lines.append(f"*Holdings (MTM) now:* ${holdings_total:.2f}")
    if breakdown:
        for b in breakdown[:12]:
            lines.append(
                f"  – {b['token']}: {b['amount']:.6f} @ ${b['price_usd']:.6f} = ${b['usd_value']:.2f}"
            )
    lines.append(f"*Unrealized PnL (open):* ${unrealized:.2f}")
    return "\n".join(lines)
