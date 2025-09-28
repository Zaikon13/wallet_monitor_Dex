# -*- coding: utf-8 -*-
from collections import defaultdict
from decimal import Decimal
from typing import List, Dict, Any

def _D(x) -> Decimal:
    return Decimal(str(x or 0))

def aggregate_per_asset(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    entries: list of {asset, side(IN/OUT), qty, usd, realized_usd}
    Returns rows with:
      asset, in_qty, in_usd, out_qty, out_usd, realized_usd, net_qty, net_usd, tx_count
    """
    acc: Dict[str, Dict[str, Decimal]] = {}
    for e in entries or []:
        asset = (e.get("asset") or "?").upper()
        side  = (e.get("side") or "IN").upper()
        qty   = _D(e.get("qty"))
        usd   = _D(e.get("usd"))
        real  = _D(e.get("realized_usd"))

        cur = acc.get(asset)
        if not cur:
            cur = {
                "asset": asset,
                "in_qty": _D(0), "in_usd": _D(0),
                "out_qty": _D(0), "out_usd": _D(0),
                "realized_usd": _D(0),
                "net_qty": _D(0), "net_usd": _D(0),
                "tx_count": _D(0),
            }
            acc[asset] = cur

        if side == "IN":
            cur["in_qty"] += qty
            cur["in_usd"] += usd
        else:
            cur["out_qty"] += qty
            cur["out_usd"] += usd

        cur["realized_usd"] += real
        # net_usd = buy positive, sell negative (entries carry signed usd already)
        cur["net_qty"] += (qty if side == "IN" else -qty)
        cur["net_usd"] += usd
        cur["tx_count"] += _D(1)

    rows = []
    for a, r in acc.items():
        rows.append({
            "asset": a,
            "in_qty": +r["in_qty"],
            "in_usd": +r["in_usd"],
            "out_qty": +r["out_qty"],
            "out_usd": +r["out_usd"],
            "realized_usd": +r["realized_usd"],
            "net_qty": +r["net_qty"],
            "net_usd": +r["net_usd"],
            "tx_count": int(r["tx_count"]),
        })
    # sort by abs realized desc then by abs net_usd desc
    rows.sort(key=lambda x: (abs(x["realized_usd"]), abs(x["net_usd"])), reverse=True)
    return rows
