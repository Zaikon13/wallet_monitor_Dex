from decimal import Decimal
from datetime import timedelta
from core.tz import now_gr
from reports.ledger import read_ledger
from reports.aggregates import aggregate_per_asset, totals
from core.guards import set_holdings
def _f(x): d=Decimal(str(x)); return f"{d:,.4f}" if abs(d)>=1 else f"{d:.6f}"
def build_weekly_report_text(wallet=None, days=7)->str:
    end=now_gr(); dates=[(end-timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)][::-1]
    es=[]; [es.extend(read_ledger(d)) for d in dates]
    rows=aggregate_per_asset(es, wallet=wallet); set_holdings({r["asset"] for r in rows})
    out=[f"🧾 Weekly PnL (last {days} days ending {end.strftime('%Y-%m-%d')})",""]
    if not rows: out.append("No entries in selected period."); return "\n".join(out)
    for r in rows:
        out.append(f"• {r['asset']}: IN { _f(r['in_qty']) } ($ { _f(r['in_usd']) }) / OUT { _f(r['out_qty']) } ($ { _f(r['out_usd']) }) / NET { _f(r['net_qty']) } ($ { _f(r['net_usd']) }) / realized $ { _f(r['realized_usd']) }")
    pos=[r for r in rows if Decimal(str(r['realized_usd']))>0]; pos.sort(key=lambda r: Decimal(str(r['realized_usd'])), reverse=True)
    neg=[r for r in rows if Decimal(str(r['realized_usd']))<0]; neg.sort(key=lambda r: Decimal(str(r['realized_usd'])))
    if pos: out+=["","🏆 Top Winners:"]+[f"  {r['asset']} +${_f(r['realized_usd'])}" for r in pos[:3]]
    if neg: out+=["","💀 Top Losers:"]+[f"  {r['asset']} ${_f(r['realized_usd'])}" for r in neg[:3]]
    t=totals(rows)
    out+=["","Totals — IN {inq} (${inusd}) | OUT {outq} (${outusd}) | NET {netq} (${netusd}) | Realized ${real}".format(
        inq=_f(t['in_qty']), inusd=_f(t['in_usd']), outq=_f(t['out_qty']), outusd=_f(t['out_usd']), netq=_f(t['net_qty']), netusd=_f(t['net_usd']), real=_f(t['realized_usd']))]
    return "\n".join(out)
