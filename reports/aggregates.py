# reports/aggregates.py
from __future__ import annotations
from typing import Iterable, Dict, Any, List
from decimal import Decimal


Number = Decimal | float | int


def _D(x: Number) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def aggregate_per_asset(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Περιμένει entries σαν dicts που να έχουν ΠΑΝΤΑ:
      - 'asset': str
      - 'side': 'IN' ή 'OUT'
      - 'qty': number
      - 'usd': number  (ποσό σε USD με πρόσημο, π.χ. OUT αρνητικό)
    Προαιρετικά:
      - 'realized_usd': number (θα αθροιστεί αν υπάρχει)

    Επιστρέφει λίστα από γραμμές:
      { asset, in_qty, in_usd, out_qty, out_usd, net_qty, net_usd, tx_count, realized_usd }
    Τα qty/usd είναι Decimal.
    """
    book: Dict[str, Dict[str, Decimal]] = {}

    for e in entries:
        asset = str(e.get("asset", "")).strip() or "UNKNOWN"
        side = str(e.get("side", "")).upper()
        qty = _D(e.get("qty", 0))
        usd = _D(e.get("usd", 0))
        realized = _D(e.get("realized_usd", 0))

        row = book.setdefault(asset, {
            "in_qty": Decimal("0"),
            "in_usd": Decimal("0"),
            "out_qty": Decimal("0"),
            "out_usd": Decimal("0"),
            "tx_count": Decimal("0"),
            "realized_usd": Decimal("0"),
        })

        if side == "IN":
            row["in_qty"] += qty
            row["in_usd"] += usd
        elif side == "OUT":
            # qty σε OUT το περιμένουμε θετικό στο input (και εμείς το κάνουμε αρνητικό στο net)
            row["out_qty"] += qty
            row["out_usd"] += usd
        else:
            # αγνόησε άγνωστο side
            continue

        row["tx_count"] += 1
        row["realized_usd"] += realized

    rows: List[Dict[str, Any]] = []
    for asset, r in book.items():
        net_qty = r["in_qty"] - r["out_qty"]
        net_usd = r["in_usd"] - r["out_usd"]
        rows.append({
            "asset": asset,
            "in_qty": r["in_qty"],
            "in_usd": r["in_usd"],
            "out_qty": r["out_qty"],
            "out_usd": r["out_usd"],
            "net_qty": net_qty,
            "net_usd": net_usd,
            "tx_count": int(r["tx_count"]),
            "realized_usd": r["realized_usd"],
        })

    # Ταξινόμηση: desc κατά |net_usd|, ώστε τα πιο “σημαντικά” να έρχονται πάνω
    rows.sort(key=lambda x: (abs(x["net_usd"]), x["asset"]), reverse=True)
    return rows
