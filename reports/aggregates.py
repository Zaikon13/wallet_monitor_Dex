# -*- coding: utf-8 -*-
from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping, MutableMapping


def _as_decimal(value: object) -> Decimal:
    """Coerce *value* into :class:`~decimal.Decimal` safely."""

    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def aggregate_per_asset(entries: Iterable[Mapping[str, object]] | None):
    """Aggregate trade entries per asset.

    Parameters
    ----------
    entries:
        Iterable of dictionaries describing fills. Each entry may contain the
        keys ``asset``, ``side`` (``"IN"`` or ``"OUT"``), ``qty``, ``usd``, and
        ``realized_usd``.

    Returns
    -------
    list[dict[str, Decimal | int]]
        Aggregated metrics per asset including totals for quantities, USD
        values, realized PnL, net deltas, and transaction counts.
    """

    totals: dict[str, MutableMapping[str, Decimal | int]] = {}

    for entry in entries or []:
        asset = str(entry.get("asset") or "?").upper()
        bucket = totals.setdefault(
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

        side = str(entry.get("side") or "IN").upper()
        qty = _as_decimal(entry.get("qty"))
        usd = _as_decimal(entry.get("usd"))
        realized = _as_decimal(entry.get("realized_usd"))

        if side == "OUT":
            bucket["out_qty"] += qty
            bucket["out_usd"] += usd
        else:
            bucket["in_qty"] += qty
            bucket["in_usd"] += usd

        bucket["realized_usd"] += realized
        bucket["tx_count"] = int(bucket["tx_count"]) + 1

    results = []
    for asset, bucket in totals.items():
        in_qty = bucket["in_qty"]
        out_qty = bucket["out_qty"]
        in_usd = bucket["in_usd"]
        out_usd = bucket["out_usd"]
        net_qty = in_qty - out_qty
        net_usd = in_usd - out_usd

        results.append(
            {
                "asset": asset,
                "in_qty": in_qty,
                "in_usd": in_usd,
                "out_qty": out_qty,
                "out_usd": out_usd,
                "net_qty": net_qty,
                "net_usd": net_usd,
                "realized_usd": bucket["realized_usd"],
                "tx_count": int(bucket["tx_count"]),
            }
        )

    results.sort(
        key=lambda row: (
            abs(row["net_usd"])
            + abs(row["in_usd"])
            + abs(row["out_usd"])
        ),
        reverse=True,
    )
    return results
