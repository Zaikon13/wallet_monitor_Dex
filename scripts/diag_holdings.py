#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os

os.environ.setdefault("DEBUG_HOLDINGS", "1")
from core.holdings import get_wallet_snapshot_debug


def main() -> int:
    snap = get_wallet_snapshot_debug()
    assets = snap.get("assets", [])
    if not assets:
        print("DIAG: snapshot empty")
    else:
        print(f"DIAG: assets={len(assets)}")
        for a in assets[:15]:
            print(f" - {a['symbol']}: amt={a['amount']} val=${a['value_usd']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
