#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-off wallet snapshot.
- Prints snapshot to stdout.
- Optionally sends snapshot to Telegram.
"""
from __future__ import annotations

import sys
from decimal import Decimal

from core.holdings import get_wallet_snapshot, format_snapshot_lines
from telegram.api import send_telegram


def main() -> None:
    snapshot = get_wallet_snapshot()
    if not snapshot:
        print("No holdings.")
        return

    text = "💰 Wallet Snapshot\n" + format_snapshot_lines(snapshot)
    print(text)

    # Fire-and-forget; send_telegram returns None (no unpacking)
    try:
        send_telegram(text)
    except Exception as e:
        print(f"⚠️ Failed to send Telegram message: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
