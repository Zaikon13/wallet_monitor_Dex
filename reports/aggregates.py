# -*- coding: utf-8 -*-
"""
Aggregate helpers for totals per asset.
Adds net_qty, net_usd, and tx_count to the classic IN/OUT/REALIZED fields.
"""
from decimal import Decimal

def aggregate_per_asset(entries):
    """
    entries: list of {asset, side(IN/OUT), qty, usd, realized_usd}
    Returns rows with:
      asset, in_qty, in_usd, out_qty, out_usd, realized_usd, net_qty, net_usd, tx_count
    """
    acc = {}
    for e in entries or []:
        asset = (e.get("asset") or "?").upper()
        side  = (e.get("side")  or "IN").upper()
        qty   = Decimal(str(e.get("qty") or 0))
        usd   = Decimal(str(e.get("usd") or 0))
        real  = Decimal(str(e.get("realized_usd") or 0))
        cur = acc.get(asset, {
            "asset": asset,
            "in_qty": Decimal("0"), "in_usd": Decimal("0"),
            "out_qty": Decimal("0"), "out_usd": Decimal("0"),
            "realized_usd": Decimal("0"),
            "tx_count": 0,
        })
        if side == "IN":
            cur["in_qty"] += qty
            cur["in_usd"] += usd
        else:
            cur["out_qty"] += qty
            # preserve sign on usd for OUT if caller already uses negative — otherwise subtract
            cur["out_usd"] += usd if usd < 0 else -usd
        cur["realized_usd"] += real
        cur["tx_count"] += 1
        acc[asset] = cur

    rows = []
    for a, r in acc.items():
        net_qty = r["in_qty"] - r["out_qty"]
        net_usd = r["in_usd"] + r["out_usd"]
        rows.append({
            "asset": a,
            "in_qty": float(r["in_qty"]), "in_usd": float(r["in_usd"]),
            "out_qty": float(r["out_qty"]), "out_usd": float(r["out_usd"]),
            "realized_usd": float(r["realized_usd"]),
            "net_qty": float(net_qty), "net_usd": float(net_usd),
            "tx_count": int(r["tx_count"]),
        })
    rows.sort(key=lambda x: abs(x["net_usd"]) + abs(x["in_usd"]) + abs(x["out_usd"]), reverse=True)
    return rows
