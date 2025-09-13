def aggregate_per_asset(rows: list[dict], by_addr: bool = False) -> list[dict]:
    """
    rows: [{"asset": str, "side": "IN"/"OUT", "qty": float, "usd": float, "realized_usd": float,
            "token_addr": Optional[str]}]
    """
    acc = {}
    for r in rows:
        asset = (r.get("asset") or "?").upper()
        addr  = r.get("token_addr") if by_addr else None
        addr_key = (addr.lower() if isinstance(addr, str) and addr.startswith("0x") else None)
        key   = (asset, addr_key)
        side  = (r.get("side") or "").upper()

        cur = acc.get(key, {
            "asset": asset,
            "token_addr": addr_key,
            "in_qty": 0.0, "in_usd": 0.0,
            "out_qty": 0.0, "out_usd": 0.0,
            "realized_usd": 0.0
        })
        if side == "IN":
            cur["in_qty"] += float(r.get("qty") or 0.0)
            cur["in_usd"] += float(r.get("usd") or 0.0)
        elif side == "OUT":
            cur["out_qty"] += float(r.get("qty") or 0.0)
            cur["out_usd"] += float(r.get("usd") or 0.0)
        cur["realized_usd"] += float(r.get("realized_usd") or 0.0)
        acc[key] = cur

    out = list(acc.values())
    out.sort(key=lambda x: abs(x["in_usd"]) + abs(x["out_usd"]), reverse=True)
    return out
