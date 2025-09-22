from collections import defaultdict
from decimal import Decimal
from typing import List, Dict, Any, Optional


def aggregate_per_asset(entries: List[Dict[str, Any]], wallet: Optional[str] = None) -> List[Dict[str, Decimal]]:
    """Aggregate entries per asset, optionally filtered by wallet.
    Storage invariant: we *do not* mix at storage; aggregation happens here (report layer).
    Each entry is expected to have: wallet, asset, side (IN/OUT), qty, usd, realized_usd
    """
    acc: Dict[str, Dict[str, Decimal]] = {}
    for e in entries or []:
        if wallet and (e.get("wallet") or "").lower() != wallet.lower():
            continue
        asset = (e.get("asset") or "?").upper()
        side = (e.get("side") or "IN").upper()
        qty = Decimal(str(e.get("qty") or 0))
        usd = Decimal(str(e.get("usd") or 0))
        real = Decimal(str(e.get("realized_usd") or 0))
        cur = acc.get(
            asset,
            {
                "asset": asset,
                "in_qty": Decimal("0"),
                "in_usd": Decimal("0"),
                "out_qty": Decimal("0"),
                "out_usd": Decimal("0"),
                "realized_usd": Decimal("0"),
                "tx_count": 0,
            },
        )
        if side == "IN":
            cur["in_qty"] += qty
            cur["in_usd"] += usd
        else:
            cur["out_qty"] += qty
            cur["out_us"] = cur.get("out_usd", Decimal("0"))  # compat safeguard
            cur["out_usd"] += usd
        cur["realized_usd"] += real
        cur["tx_count"] += 1
        acc[asset] = cur

    rows: List[Dict[str, Decimal]] = []
    for a, r in acc.items():
        r["net_qty"] = r["in_qty"] - r["out_qty"]
        r["net_usd"] = r["in_usd"] - r["out_usd"]
        rows.append(r)
    return rows


def totals(rows: List[Dict[str, Decimal]]) -> Dict[str, Decimal]:
    z = {k: Decimal("0") for k in ["in_qty","out_qty","net_qty","in_usd","out_usd","net_usd","realized_usd","tx_count"]}
    for r in rows:
        z["in_qty"] += r.get("in_qty", Decimal("0"))
        z["out_qty"] += r.get("out_qty", Decimal("0"))
        z["net_qty"] += r.get("net_qty", Decimal("0"))
        z["in_usd"] += r.get("in_usd", Decimal("0"))
        z["out_usd"] += r.get("out_usd", Decimal("0"))
        z["net_usd"] += r.get("net_usd", Decimal("0"))
        z["realized_usd"] += r.get("realized_usd", Decimal("0"))
        z["tx_count"] += r.get("tx_count", 0)
    return z
