from decimal import Decimal


def _dec(value) -> Decimal:
    return Decimal(str(value or 0))


def aggregate_per_asset(entries, wallet=None):
    acc = {}
    for entry in entries:
        if wallet and (entry.get("wallet") or "").lower() != wallet.lower():
            continue
        asset = (entry.get("asset") or "?").upper()
        bucket = acc.setdefault(
            asset,
            {
                "in_qty": Decimal("0"),
                "out_qty": Decimal("0"),
                "in_usd": Decimal("0"),
                "out_usd": Decimal("0"),
                "realized_usd": Decimal("0"),
                "tx_count": 0,
            },
        )

        side = (entry.get("side") or "").upper()
        qty = _dec(entry.get("qty"))
        usd = _dec(entry.get("usd"))
        if side == "IN":
            bucket["in_qty"] += qty
            bucket["in_usd"] += usd
        elif side == "OUT":
            bucket["out_qty"] += qty
            bucket["out_usd"] += usd

        bucket["realized_usd"] += _dec(entry.get("realized_usd"))
        bucket["tx_count"] += 1

    rows = []
    for asset, values in acc.items():
        net_qty = values["in_qty"] - values["out_qty"]
        net_usd = values["in_usd"] - values["out_usd"]
        rows.append(
            {
                "asset": asset,
                "in_qty": values["in_qty"],
                "out_qty": values["out_qty"],
                "in_usd": values["in_usd"],
                "out_usd": values["out_usd"],
                "realized_usd": values["realized_usd"],
                "tx_count": values["tx_count"],
                "net_qty": net_qty,
                "net_usd": net_usd,
            }
        )

    return rows


def totals(rows):
    totals_map = {
        "in_qty": Decimal("0"),
        "out_qty": Decimal("0"),
        "in_usd": Decimal("0"),
        "out_usd": Decimal("0"),
        "net_qty": Decimal("0"),
        "net_usd": Decimal("0"),
        "realized_usd": Decimal("0"),
    }

    for row in rows:
        for key in totals_map:
            totals_map[key] += _dec(row.get(key))

    return totals_map
