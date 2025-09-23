# reports/weekly.py
from __future__ import annotations
from decimal import Decimal
from datetime import timedelta
from typing import Any, Dict, Iterable, List

from core.tz import now_gr
from reports.ledger import read_ledger
from reports.aggregates import aggregate_per_asset, totals

# set_holdings is optional – make it a no-op if the module is missing
try:
    from core.guards import set_holdings  # type: ignore
except Exception:  # pragma: no cover
    def set_holdings(_symbols: Iterable[str]) -> None:
        return

def _D(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal("0")

def _f(x: Any) -> str:
    d = _D(x)
    return f"{d:,.4f}" if abs(d) >= 1 else f"{d:.6f}"

def build_weekly_report_text(wallet: str | None = None, days: int = 7) -> str:
    try:
        end = now_gr()
        dates = [(end - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
        dates.sort()  # chronological (oldest -> newest)

        # Collect ledger entries across the window
        entries: List[Dict[str, Any]] = []
        for d in dates:
            try:
                chunk = read_ledger(d) or []
            except Exception:
                chunk = []
            entries.extend(chunk)

        # Aggregate
        rows = aggregate_per_asset(entries, wallet=wallet) or []
        set_holdings({(r.get("asset") or "?").upper() for r in rows})

        out: List[str] = [f"🧾 Weekly PnL (last {days} days ending {end.strftime('%Y-%m-%d')})", ""]

        if not rows:
            out.append("No entries in selected period.")
            return "\n".join(out)

        # Stable order: CRO first, then alpha
        rows.sort(key=lambda r: ((r.get("asset") or "") != "CRO", (r.get("asset") or "")))

        # Per-asset lines
        for r in rows:
            a = (r.get("asset") or "?").upper()
            out.append(
                "• {a}: IN {inq} ($ {inusd}) / OUT {outq} ($ {outusd}) / "
                "NET {netq} ($ {netusd}) / realized $ {real}".format(
                    a=a,
                    inq=_f(r.get("in_qty")),
                    inusd=_f(r.get("in_usd")),
                    outq=_f(r.get("out_qty")),
                    outusd=_f(r.get("out_usd")),
                    netq=_f(r.get("net_qty")),
                    netusd=_f(r.get("net_usd")),
                    real=_f(r.get("realized_usd")),
                )
            )

        # Winners / Losers (by realized PnL)
        pos = [r for r in rows if _D(r.get("realized_usd")) > 0]
        pos.sort(key=lambda r: _D(r.get("realized_usd")), reverse=True)
        neg = [r for r in rows if _D(r.get("realized_usd")) < 0]
        neg.sort(key=lambda r: _D(r.get("realized_usd")))

        if pos:
            out += ["", "🏆 Top Winners:"] + [f"  {r['asset']} +${_f(r.get('realized_usd'))}" for r in pos[:3]]
        if neg:
            out += ["", "💀 Top Losers:"] + [f"  {r['asset']} ${_f(r.get('realized_usd'))}" for r in neg[:3]]

        # Totals
        t = totals(rows)
        out += [
            "",
            "Totals — IN {inq} (${inusd}) | OUT {outq} (${outusd}) | "
            "NET {netq} (${netusd}) | Realized ${real}".format(
                inq=_f(t.get("in_qty")),
                inusd=_f(t.get("in_usd")),
                outq=_f(t.get("out_qty")),
                outusd=_f(t.get("out_usd")),
                netq=_f(t.get("net_qty")),
                netusd=_f(t.get("net_usd")),
                real=_f(t.get("realized_usd")),
            ),
        ]
        return "\n".join(out)

    except Exception as e:
        # Never crash callers; surface a readable error
        return f"🧾 Weekly PnL — error: {e}"
