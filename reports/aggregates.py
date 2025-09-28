from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _normalize_wallet(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def aggregate_per_asset(
    entries: Iterable[Dict[str, Any]] | None,
    wallet: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Aggregate buy/sell ledger entries per asset.

    The function keeps Decimal values so downstream formatters/tests can
    perform precise arithmetic without string parsing.
    """

    normalized_wallet = _normalize_wallet(wallet)
    acc: Dict[str, Dict[str, Decimal]] = defaultdict(
        lambda: {
            "in_qty": Decimal("0"),
            "out_qty": Decimal("0"),
            "in_usd": Decimal("0"),
            "out_usd": Decimal("0"),
            "realized_usd": Decimal("0"),
            "tx_count": Decimal("0"),
        }
    )

    for entry in entries or []:
        if not isinstance(entry, dict):
            continue

        if normalized_wallet and _normalize_wallet(entry.get("wallet")) not in {
            "",
            normalized_wallet,
        }:
            continue

        asset = str(entry.get("asset") or "?").upper()
        if not asset:
            asset = "?"

        side = str(entry.get("side") or "").upper()
        qty = _to_decimal(entry.get("qty"))
        usd = _to_decimal(entry.get("usd"))
        realized = _to_decimal(entry.get("realized_usd"))

        bucket = acc[asset]
        if side == "IN":
            bucket["in_qty"] += qty
            bucket["in_usd"] += usd
            bucket["tx_count"] += Decimal(1)
        elif side == "OUT":
            bucket["out_qty"] += qty
            bucket["out_usd"] += usd
            bucket["tx_count"] += Decimal(1)
        elif side == "SWAP":
            bucket["tx_count"] += Decimal(1)
        else:
            # ignore unsupported side but still count to highlight activity
            bucket["tx_count"] += Decimal(1)

        bucket["realized_usd"] += realized

    rows: List[Dict[str, Any]] = []
    for asset, values in acc.items():
        in_qty = values["in_qty"]
        out_qty = values["out_qty"]
        in_usd = values["in_usd"]
        out_usd = values["out_usd"]
        realized_usd = values["realized_usd"]
        tx_count = int(values["tx_count"])
        rows.append(
            {
                "asset": asset,
                "in_qty": in_qty,
                "out_qty": out_qty,
                "net_qty": in_qty - out_qty,
                "in_usd": in_usd,
                "out_usd": out_usd,
                "net_usd": in_usd - out_usd,
                "realized_usd": realized_usd,
                "tx_count": tx_count,
            }
        )

    rows.sort(key=lambda row: (row["net_usd"], row["asset"]), reverse=True)
    return rows


def totals(rows: Iterable[Dict[str, Any]]) -> Dict[str, Decimal]:
    total = {
        "in_qty": Decimal("0"),
        "out_qty": Decimal("0"),
        "net_qty": Decimal("0"),
        "in_usd": Decimal("0"),
        "out_usd": Decimal("0"),
        "net_usd": Decimal("0"),
        "realized_usd": Decimal("0"),
    }
    for row in rows or []:
        for key in total:
            total[key] += _to_decimal(row.get(key))
    return total
