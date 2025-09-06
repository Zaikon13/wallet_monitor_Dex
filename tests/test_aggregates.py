from decimal import Decimal
from reports.aggregates import aggregate_per_asset
from telegram.formatters import format_per_asset_totals


def test_aggregate_and_format_simple():
    entries = [
        {"asset": "MCGA", "side": "IN", "qty": 100, "usd": 200},
        {"asset": "MCGA", "side": "OUT", "qty": 40, "usd": 80},
        {"asset": "WAVE", "side": "IN", "qty": 10_000, "usd": 0.00002},
        {"asset": "WAVE", "side": "OUT", "qty": 5_000, "usd": 0.00001},
        {"asset": "MCGA", "side": "IN", "qty": 50, "usd": 100,
         "realized_usd": -1.5},
    ]
    rows = aggregate_per_asset(entries)
    # Βρες MCGA
    mcga = next(r for r in rows if r["asset"] == "MCGA")
    assert mcga["in_qty"] == Decimal("150")
    assert mcga["out_qty"] == Decimal("40")
    assert mcga["net_qty"] == Decimal("110")
    assert mcga["in_usd"] == Decimal("300")
    assert mcga["out_usd"] == Decimal("80")
    assert mcga["net_usd"] == Decimal("220")
    assert mcga["tx_count"] == 3
    assert mcga["realized_usd"] == Decimal("-1.5")

    # Και ένα smoke στο formatter (να μη σκάει)
    txt = format_per_asset_totals("today", rows)
    assert "Totals per Asset — Today" in txt
    assert "MCGA" in txt
    assert "TXs: 3" in txt
