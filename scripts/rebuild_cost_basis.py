#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rebuild FIFO cost basis across all ledger days and show a realized PnL summary.

Usage:
  python scripts/rebuild_cost_basis.py

Notes:
- Uses only the **public API** of reports/ledger.py:
    - update_cost_basis()  # bulk rebuild, writes annotated entries & basis file
    - iter_all_entries()   # to compute realized PnL totals for the summary
- Idempotent & safe to rerun.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from decimal import Decimal

from reports.ledger import update_cost_basis, iter_all_entries


def _fmt_dec(v: Decimal) -> str:
    if v == 0:
        return "0"
    if abs(v) >= 1:
        return f"{v:,.2f}"
    return f"{v:.6f}"


def main(argv: list[str] | None = None) -> int:
    # 1) Bulk rebuild (writes annotated entries per day + cost_basis.json)
    try:
        update_cost_basis()  # public bulk path; no args
    except Exception as e:
        print(f"ERROR: rebuild failed: {e}", file=sys.stderr)
        return 1

    # 2) Compute realized PnL totals over all (now-annotated) entries
    realized_per_asset: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    total = Decimal("0")
    count = 0

    for entry in iter_all_entries():
        asset = str(entry.get("asset") or "?").upper()
        rv = entry.get("realized_usd")
        if rv is None:
            continue
        try:
            val = Decimal(str(rv))
        except Exception:
            continue
        if val != 0:
            realized_per_asset[asset] += val
            total += val
        count += 1

    # 3) Print summary
    print("✅ Cost basis rebuild completed.")
    print(f"Entries scanned: {count}")
    if realized_per_asset:
        print("\nRealized PnL by asset:")
        for asset, val in sorted(realized_per_asset.items(), key=lambda kv: kv[1], reverse=True):
            sign = "+" if val >= 0 else ""
            print(f" - {asset}: {sign}${_fmt_dec(val)}")
    print("\nTotal realized PnL:")
    sign = "+" if total >= 0 else ""
    print(f" = {sign}${_fmt_dec(total)}")

    return 0



if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Rebuild FIFO cost basis")
    _ = ap.parse_args()
    raise SystemExit(main())
