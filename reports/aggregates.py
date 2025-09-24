from decimal import Decimal

def _D(x) -> Decimal:
    return Decimal(str(x if x is not None else 0))

def aggregate_per_asset(entries, wallet: str | None = None):
    """
    Returns per-asset rows with Decimal fields and tx_count:
      {
        "asset": "MCGA",
        "in_qty": Decimal, "out_qty": Decimal, "net_qty": Decimal,
        "in_usd": Decimal, "out_usd": Decimal, "net_usd": Decimal,
        "realized_usd": Decimal,
        "tx_count": int,
      }
    """
    acc: dict[str, dict] = {}
    for e in entries or []:
        if wallet:
            w = (e.get("wallet") or "")
            if w.lower() != wallet.lower():
                continue
        a = (e.get("asset") or "?").upper()
        cur = acc.get(a)
        if not cur:
            cur = acc[a] = {
                "in_qty": Decimal("0"),
                "out_qty": Decimal("0"),
                "in_usd": Decimal("0"),
                "out_usd": Decimal("0"),
                "realized_usd": Decimal("0"),
                "tx_count": 0,
            }
        side = (e.get("side") or "").upper()
        qty = _D(e.get("qty"))
        usd = _D(e.get("usd"))
        if side == "IN":
            cur["in_qty"] += qty
            cur["in_usd"] += usd
        elif side == "OUT":
            cur["out_qty"] += qty
            cur["out_usd"] += usd
        cur["realized_usd"] += _D(e.get("realized_usd"))
        cur["tx_count"] += 1

    rows = []
    for a, v in acc.items():
        netq = v["in_qty"] - v["out_qty"]
        netu = v["in_usd"] - v["out_usd"]
        rows.append({
            "asset": a,
            "in_qty": v["in_qty"],
            "out_qty": v["out_qty"],
            "net_qty": netq,
            "in_usd": v["in_usd"],
            "out_usd": v["out_usd"],
            "net_usd": netu,
            "realized_usd": v["realized_usd"],
            "tx_count": v["tx_count"],
        })
    return rows

def totals(rows):
    t = {k: Decimal("0") for k in ["in_qty","out_qty","in_usd","out_usd","net_qty","net_usd","realized_usd"]}
    for r in rows or []:
        t["in_qty"]       += _D(r.get("in_qty"))
        t["out_qty"]      += _D(r.get("out_qty"))
        t["in_usd"]       += _D(r.get("in_usd"))
        t["out_usd"]      += _D(r.get("out_usd"))
        t["net_qty"]      += _D(r.get("net_qty"))
        t["net_usd"]      += _D(r.get("net_usd"))
        t["realized_usd"] += _D(r.get("realized_usd"))
    return t
