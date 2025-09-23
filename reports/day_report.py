# reports/day_report.py
from __future__ import annotations
from datetime import datetime, date
from decimal import Decimal
from typing import Any, Dict, List, Optional

# ---------- Safe imports (fallbacks if modules are missing) ----------
try:
    from core.tz import now_gr, ymd  # type: ignore
except Exception:  # Fallbacks: naive local time
    def now_gr() -> datetime:  # type: ignore
        return datetime.now()
    def ymd(dt: Optional[datetime] = None) -> str:  # type: ignore
        return (dt or datetime.now()).strftime("%Y-%m-%d")

try:
    from core.holdings import get_wallet_snapshot  # type: ignore
except Exception:
    def get_wallet_snapshot(_address: str = "") -> Dict[str, Dict[str, Any]]:  # type: ignore
        return {}

try:
    from reports.ledger import read_ledger  # type: ignore
except Exception:
    def read_ledger(_date_str: str) -> List[Dict[str, Any]]:  # type: ignore
        return []

try:
    from reports.aggregates import aggregate_per_asset, totals  # type: ignore
except Exception:
    def aggregate_per_asset(entries, wallet: Optional[str] = None):  # type: ignore
        return []
    def totals(rows):  # type: ignore
        return {
            "in_qty": Decimal("0"), "out_qty": Decimal("0"), "net_qty": Decimal("0"),
            "in_usd": Decimal("0"), "out_usd": Decimal("0"), "net_usd": Decimal("0"),
            "realized_usd": Decimal("0"),
        }

# ---------- Helpers ----------
D = Decimal

def _D(x: Any) -> Decimal:
    try:
        return D(str(x))
    except Exception:
        return D("0")

def _fmt(x: Any) -> str:
    d = _D(x)
    if abs(d) >= 1:
        return f"{d:,.4f}"
    if abs(d) >= D("0.0001"):
        return f"{d:.6f}"
    return f"{d:.8f}"

def _sum(xs: List[Any]) -> Decimal:
    z = D("0")
    for x in xs or []:
        z += _D(x)
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

def _safe_read_ledger(d: str) -> List[Dict[str, Any]]:
    try:
        rows = read_ledger(d) or []
        # ensure list[dict]
        return [r for r in rows if isinstance(r, dict)]
    except Exception:
        return []

def _snapshot_amount(item: Dict[str, Any]) -> Decimal:
    # Support both schemas: amount/usd_value and qty/usd
    if not isinstance(item, dict):
        return D("0")
    if "amount" in item:
        return _D(item.get("amount"))
    return _D(item.get("qty"))

def _snapshot_usd(item: Dict[str, Any]) -> Decimal:
    if not isinstance(item, dict):
        return D("0")
    if "usd_value" in item:
        return _D(item.get("usd_value"))
    return _D(item.get("usd"))

# ---------- Main API ----------
def build_intraday_report_text(wallet: Optional[str] = None, date_str: Optional[str] = None) -> str:
    d = date_str or ymd()
    ts = now_gr().strftime("%Y-%m-%d %H:%M:%S")

    # Ledger entries (safe)
    entries = _safe_read_ledger(d)
    if wallet:
        wl = wallet.lower()
        entries = [e for e in entries if (e.get("wallet") or "").lower() == wl]

    lines: List[str] = []
    lines.append("🟡 Intraday Update")
    lines.append(f"_Generated: {ts}_")
    lines.append("")

    # Transactions (latest first up to ~20)
    txs = entries[-20:] if entries else []
    if txs:
        lines.append("Transactions:")
        for e in txs:
            try:
                lines.append(_tx_line(e))
            except Exception:
                # Skip malformed entries
                continue
        lines.append("")

    # Aggregates per token
    try:
        rows = aggregate_per_asset(entries, wallet=wallet) or []
    except Exception:
        rows = []
    rows.sort(key=lambda r: ((r.get("asset") or "") != "CRO", (r.get("asset") or "")))

    if rows:
        lines.append("Per-Asset (Today):")
        for r in rows:
            try:
                a = (r.get("asset") or "?").upper()
                lines.append(
                    f"• {a}  |  IN { _fmt(r.get('in_qty')) } (${ _fmt(r.get('in_usd')) })"
                    f"  /  OUT { _fmt(r.get('out_qty')) } (${ _fmt(r.get('out_usd')) })"
                    f"  /  NET { _fmt(r.get('net_qty')) } (${ _fmt(r.get('net_usd')) })"
                    f"  /  realized ${ _fmt(r.get('realized_usd')) }"
                )
            except Exception:
                continue

        try:
            t = totals(rows)
        except Exception:
            t = {
                "in_qty": D("0"), "out_qty": D("0"), "net_qty": D("0"),
                "in_usd": D("0"), "out_usd": D("0"), "net_usd": D("0"),
                "realized_usd": D("0"),
            }
        lines.append("")
        lines.append(
            "Totals — IN {inq} (${inusd}) | OUT {outq} (${outusd}) | NET {netq} (${netusd}) | Realized ${real}".format(
                inq=_fmt(t.get("in_qty")), inusd=_fmt(t.get("in_usd")),
                outq=_fmt(t.get("out_qty")), outusd=_fmt(t.get("out_usd")),
                netq=_fmt(t.get("net_qty")), netusd=_fmt(t.get("net_usd")),
                real=_fmt(t.get("realized_usd")),
            )
        )
        lines.append("")

    # Holdings MTM now
    try:
        snap = get_wallet_snapshot(wallet or "")
    except Exception:
        snap = {}
    if isinstance(snap, dict) and snap:
        lines.append("Holdings (MTM) now:")
        for sym in sorted(snap.keys()):
            row = snap.get(sym, {})
            try:
                amt = _fmt(_snapshot_amount(row))
                val = _fmt(_snapshot_usd(row))
                # optional: price = usd/amt
                price = ""
                try:
                    amt_dec = _snapshot_amount(row)
                    if amt_dec and amt_dec != D("0"):
                        p = _snapshot_usd(row) / amt_dec
                        price = f" @ ${_fmt(p)}"
                except Exception:
                    pass
                lines.append(f"  – {sym}: {amt}{price} = ${val}")
            except Exception:
                continue
        lines.append("")

    return "\n".join(lines)

def build_day_report_text(date_str: Optional[str] = None, wallet: Optional[str] = None) -> str:
    # Daily (end-of-day) — concise summary reusing the same machinery
    d = date_str or ymd()
    ts = now_gr().strftime("%Y-%m-%d %H:%M:%S")

    entries = _safe_read_ledger(d)
    if wallet:
        wl = wallet.lower()
        entries = [e for e in entries if (e.get("wallet") or "").lower() == wl]

    try:
        rows = aggregate_per_asset(entries, wallet=wallet) or []
    except Exception:
        rows = []
    rows.sort(key=lambda r: ((r.get("asset") or "") != "CRO", (r.get("asset") or "")))

    if not rows:
        return f"📒 Daily Report ({d})\nNo entries.\n_Generated: {ts}_"

    try:
        t = totals(rows)
    except Exception:
        t = {
            "in_qty": D("0"), "out_qty": D("0"), "net_qty": D("0"),
            "in_usd": D("0"), "out_usd": D("0"), "net_usd": D("0"),
            "realized_usd": D("0"),
        }

    lines: List[str] = []
    lines.append(f"📒 Daily Report ({d})")
    lines.append(f"_Generated: {ts}_\n")
    lines.append("*Per-asset:*")
    for r in rows:
        try:
            a = (r.get("asset") or "?").upper()
            lines.append(
                f"• {a}: IN { _fmt(r.get('in_qty')) } (${ _fmt(r.get('in_usd')) })"
                f" / OUT { _fmt(r.get('out_qty')) } (${ _fmt(r.get('out_usd')) })"
                f" / NET { _fmt(r.get('net_qty')) } (${ _fmt(r.get('net_usd')) })"
                f" / realized ${ _fmt(r.get('realized_usd')) }"
            )
        except Exception:
            continue
    lines.append("")
    lines.append(
        "Totals — IN {inq} (${inusd}) | OUT {outq} (${outusd}) | NET {netq} (${netusd}) | Realized ${real}".format(
            inq=_fmt(t.get("in_qty")), inusd=_fmt(t.get("in_usd")),
            outq=_fmt(t.get("out_qty")), outusd=_fmt(t.get("out_usd")),
            netq=_fmt(t.get("net_qty")), netusd=_fmt(t.get("net_usd")),
            real=_fmt(t.get("realized_usd")),
        )
    )
    return "\n".join(lines)
