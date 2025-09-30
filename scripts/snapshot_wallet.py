#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-off wallet snapshot (CRO + ERC20 on Cronos) with optional Telegram send.
Reads env: WALLET_ADDRESS, RPC_URL, DEX_PAIRS, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TZ
"""
from __future__ import annotations
import os, sys
from datetime import datetime
from zoneinfo import ZoneInfo

# --- imports από το repo σου (τα έχουμε ήδη «πράσινα») ---
from core.holdings import get_wallet_snapshot, format_snapshot_lines
from telegram.api import send_telegram


def _tz():
    try:
        return ZoneInfo(os.getenv("TZ", "Europe/Athens"))
    except Exception:
        return ZoneInfo("UTC")


def main() -> int:
    tz = _tz()
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z")

    # sanity check στα απαραίτητα
    wallet = os.getenv("WALLET_ADDRESS")
    if not wallet:
        print("ERROR: Missing WALLET_ADDRESS in env", file=sys.stderr)
        return 1
    if not (os.getenv("RPC_URL") or os.getenv("CRONOS_RPC_URL")):
        print("ERROR: Missing RPC_URL (or CRONOS_RPC_URL) in env", file=sys.stderr)
        return 1

    try:
        snap = get_wallet_snapshot()
    except Exception as e:
        print(f"ERROR building snapshot: {e}", file=sys.stderr)
        return 1

    lines = [f"🧾 Wallet Snapshot — {now}", ""]
    lines.extend(format_snapshot_lines(snap))
    text = "\n".join(lines)

    # Εκτύπωση στο stdout (θα το μαζέψει το workflow και θα το βάλει σε Issue)
    print(text)

    # Προαιρετικά: στείλ’ το και στο Telegram αν υπάρχουν credentials
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"):
        try:
            send_telegram(text)
        except Exception as exc:
            print(f"ERROR sending Telegram message: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
