from decimal import Decimal
def _D(x): return Decimal(str(x or 0))
def aggregate_per_asset(entries, wallet=None):
    acc={}
    for e in entries:
        if wallet and (e.get("wallet") or "").lower()!=wallet.lower(): continue
        a=(e.get("asset") or "?").upper()
        acc.setdefault(a, {"in_qty":_D(0),"out_qty":_D(0),"in_usd":_D(0),"out_usd":_D(0),"realized_usd":_D(0)})
        side=(e.get("side") or "").upper(); qty=_D(e.get("qty")); usd=_D(e.get("usd"))
        if side=="IN": acc[a]["in_qty"]+=qty; acc[a]["in_usd"]+=usd
        elif side=="OUT": acc[a]["out_qty"]+=qty; acc[a]["out_usd"]+=usd
        acc[a]["realized_usd"]+=_D(e.get("realized_usd"))
    rows=[]
    for a,v in acc.items():
        netq=v["in_qty"]-v["out_qty"]; netu=v["in_usd"]-v["out_usd"]
        rows.append({"asset":a, **{k:str(vv) for k,vv in v.items()}, "net_qty":str(netq), "net_usd":str(netu)})
    return rows
def totals(rows):
    from decimal import Decimal
    t={k:Decimal("0") for k in ["in_qty","out_qty","in_usd","out_usd","net_qty","net_usd","realized_usd"]}
    for r in rows:
        for k in t: t[k]+=Decimal(str(r.get(k) or 0))
    return {k:str(v) for k,v in t.items()}
