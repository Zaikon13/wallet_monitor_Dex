from decimal import Decimal
from core.tz import ymd
from reports.ledger import read_ledger
from reports.aggregates import aggregate_per_asset, totals
def _f(x): d=Decimal(str(x)); return f"{d:,.4f}" if abs(d)>=1 else f"{d:.6f}"
def build_day_report_text(intraday=False, wallet=None)->str:
    day=ymd(); es=read_ledger(day); rows=aggregate_per_asset(es,wallet); t=totals(rows)
    title="🕒 Intraday Update" if intraday else f"📒 Daily Report ({day})"
    out=[title,""]
    if not es: out.append("No transactions yet today.")
    else:
        out.append("Per-asset today:")
        for r in rows:
            out.append(f"  • {r['asset']}: NET { _f(r['net_qty']) } ($ { _f(r['net_usd']) }) | realized $ { _f(r['realized_usd']) }")
    out.append("")
    out.append("Totals: IN ${} / OUT ${} / NET ${} / Realized ${}".format(_f(t['in_usd']),_f(t['out_usd']),_f(t['net_usd']),_f(t['realized_usd'])))
    return "\n".join(out)
