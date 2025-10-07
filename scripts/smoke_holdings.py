#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quick smoke for core/holdings_adapters & snapshot schema.
Prints CRO and tCRO lines distinctly, plus totals.
"""
from __future__ import annotations

import argparse
import json
import sys

from core.holdings_adapters import build_holdings_snapshot


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Holdings smoke")
    p.add_argument("--limit", type=int, default=20, help="max assets to show")
    p.add_argument("--json", action="store_true", help="print raw JSON snapshot")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    snap = build_holdings_snapshot(base_ccy="USD")

    if args.json:
        print(json.dumps(snap, indent=2, sort_keys=True))
        return 0

    assets = snap.get("assets", [])[: max(1, args.limit)]
    totals = snap.get("totals", {})
    print("\ud83d\udcca Holdings (smoke)")
    for a in assets:
        sym = a.get("symbol", "?")
        native = a.get("native", False)
        mark = " (CRO-native)" if native and sym == "CRO" else ""
        print(
            f"- {sym}{mark}  amt={a.get('amount','0')}  "
            f"px=${a.get('price_usd','0')}  val=${a.get('value_usd','0')}  "
            f"\u0394=${a.get('u_pnl_usd','0')} ({a.get('u_pnl_pct','0')}%)"
        )
    print(
        f"— Totals: val=${totals.get('value_usd','0')}  "
        f"cost=${totals.get('cost_usd','0')}  "
        f"\u0394=${totals.get('u_pnl_usd','0')} ({totals.get('u_pnl_pct','0')}%)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
