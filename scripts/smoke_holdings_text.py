#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from core.holdings import get_wallet_snapshot

def main() -> int:
    snap = get_wallet_snapshot()
    assets = snap.get("assets", [])
    if not assets:
        print("Holdings snapshot: (empty)")  # CI visibility
    else:
        print("Top assets:")
        for a in assets[:10]:
            print(f" - {a['symbol']}: amt={a['amount']}  val=${a['value_usd']}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
