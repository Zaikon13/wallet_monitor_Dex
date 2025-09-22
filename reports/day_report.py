from datetime import datetime
from decimal import Decimal
from typing import List, Dict, Any, Optional

from core.tz import now_gr, ymd
from core.holdings import get_wallet_snapshot
from reports.ledger import read_ledger
from reports.aggregates import aggregate_per_asset, totals

def _fmt(a: Decimal) -> str:
    try:
        a = Decimal(str(a))
    except Exception:
        return str(a)
    if abs(a) >= 1:
        return f"{a:,.4f}"
    if abs(a) >= Decimal("0.0001"):
        return f"{a:.6f}"
    return f"{a:.8f}"

def _sum(xs):
    z = Decimal("0")
    for x in xs:
        try: z += Decimal(str(x))
        except: pass
    return z

def _tx_line(e: Dict[str, Any]) -> str:
    # expects time(e.g. "03:03:20"), side IN/OUT, asset, qty, price_usd, usd
    t  = e.get("time") or e.get("ts") or ""
    sd = (e.get("side") or "").upper()
    a  = (e.get("asset") or "?").upper()
    q  = _fmt(e.get("qty") or 0)
    pu = _fmt(e.get("price_usd") or 0)
    u  = _fmt(e.get("usd") or 0)
    sign = "" if sd == "IN" else "-"
    return f"• {t} — {sd} {a} {q}  @ ${pu}  (${sign}{u})"

def build_intraday_report_text(wallet: Optional[str] = None, date_str: Optional[str] = None) -> str:
    d = date_str or ymd()
    ts = now_gr().strftime("%Y-%m-%d %H:%M:%S")

    entries = read_ledger(d)
    if wallet:
        entries = [e for e in entries if (e.get("wallet") or "").lower() == wallet.lower()]

    lines: List[str] = []
    lines.append("🟡 Intraday Update")
    lines.append(f"_Generated: {ts}_")
    lines.append("")

    # Transactions (latest first up to ~20)
    txs = entries[-20:]
    if txs:
        lines.append("Transactions:")
        for e in txs:
            lines.append(_tx_line(e))
        lines.append("")

    # Aggregates per token
    rows = aggregate_per_asset(entries, wallet=wallet)
    rows.sort(key=lambda r: (r.get("asset") != "CRO", r.get("asset")))
    if rows:
        lines.append("Per-Asset (Today):")
        for r in rows:
            a = r["asset"]
            lines.append(
                f"• {a}  |  IN { _fmt(r['in_qty']) } (${ _fmt(r['in_usd']) })"
                f"  /  OUT { _fmt(r['out_qty']) } (${ _fmt(r['out_usd']) })"
                f"  /  NET { _fmt(r['net_qty']) } (${ _fmt(r['net_usd']) })"
                f"  /  realized ${ _fmt(r['realized_usd']) }"
            )
        t = totals(rows)
        lines.append("")
        lines.append(
            "Totals — IN {inq} (${inusd}) | OUT {outq} (${outusd}) | NET {netq} (${netusd}) | Realized ${real}".format(
                inq=_fmt(t["in_qty"]), inusd=_fmt(t["in_usd"]),
                outq=_fmt(t["out_qty"]), outusd=_fmt(t["out_usd"]),
                netq=_fmt(t["net_qty"]), netusd=_fmt(t["net_usd"]),
                real=_fmt(t["realized_usd"]),
            )
        )
        lines.append("")

    # Holdings MTM now
    if wallet:
        snap = get_wallet_snapshot(wallet)
    else:
        snap = get_wallet_snapshot("")  # fallback — implementation may ignore empty
    if snap:
        lines.append("Holdings (MTM) now:")
        for sym, row in sorted(snap.items()):
            amt = _fmt(row["amount"])
            # usd_value is already MTM value
            val = _fmt(row.get("usd_value", 0))
            price = ""
            # optional: price could be derived = usd/amt (if amt>0)
            try:
                if row["amount"] and row["amount"] != Decimal("0"):
                    p = (row.get("usd_value", Decimal("0")) / row["amount"])
                    price = f" @ ${_fmt(p)}"
            except Exception:
                pass
            lines.append(f"  – {sym}: {amt}{price} = ${val}")
        lines.append("")

    return "\n".join(lines)

def build_day_report_text(date_str: Optional[str] = None, wallet: Optional[str] = None) -> str:
    # Daily (end-of-day) — concise summary reusing the same machinery
    d = date_str or ymd()
    ts = now_gr().strftime("%Y-%m-%d %H:%M:%S")
    entries = read_ledger(d)
    if wallet:
        entries = [e for e in entries if (e.get("wallet") or "").lower() == wallet.lower()]

    rows = aggregate_per_asset(entries, wallet=wallet)
    rows.sort(key=lambda r: (r.get("asset") != "CRO", r.get("asset")))

    if not rows:
        return f"📒 Daily Report ({d})\nNo entries.\n_Generated: {ts}_"

    t = totals(rows)
    lines: List[str] = []
    lines.append(f"📒 Daily Report ({d})")
    lines.append(f"_Generated: {ts}_\n")
    lines.append("*Per-asset:*")
    for r in rows:
        a = r["asset"]
        lines.append(
            f"• {a}: IN { _fmt(r['in_qty']) } (${ _fmt(r['in_usd']) })"
            f" / OUT { _fmt(r['out_qty']) } (${ _fmt(r['out_usd']) })"
            f" / NET { _fmt(r['net_qty']) } (${ _fmt(r['net_usd']) })"
            f" / realized ${ _fmt(r['realized_usd']) }"
        )
    lines.append("")
    lines.append(
        "Totals — IN {inq} (${inusd}) | OUT {outq} (${outusd}) | NET {netq} (${netusd}) | Realized ${real}".format(
            inq=_fmt(t["in_qty"]), inusd=_fmt(t["in_usd"]),
            outq=_fmt(t["out_qty"]), outusd=_fmt(t["out_usd"]),
            netq=_fmt(t["net_qty"]), netusd=_fmt(t["net_usd"]),
            real=_fmt(t["realized_usd"]),
        )
    )
    return "\n".join(lines)
