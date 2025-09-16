#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — minimal, stable runtime
- One-shot ή loop (intraday + EOD)
- Watchlist από DEX_PAIRS (addresses ή symbols, comma-separated)
- Ασφαλή Telegram μηνύματα (χωρίς MarkdownV2 by default)
- Safe pricing μέσω core/pricing.get_price_usd (ποτέ δεν πετάει exceptions)
"""

from __future__ import annotations
import os
import sys
import time
import argparse
import logging
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:
    ZoneInfo = None  # type: ignore

from telegram.api import send_telegram
from core.pricing import get_price_usd, HISTORY_LAST_PRICE

APP_NAME = "Cronos DeFi Sentinel (minimal)"

# -------------------- ENV --------------------
TZ = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS = float(os.getenv("INTRADAY_HOURS", "3"))  # κάθε πόσες ώρες intraday
EOD_TIME = os.getenv("EOD_TIME", "23:59")                 # HH:MM τοπικής ζώνης
WALLET_ADDRESS = (os.getenv("WALLET_ADDRESS") or "").lower()
DEX_PAIRS = os.getenv("DEX_PAIRS", "")  # π.χ. "cronos/0xPAIR1,cronos/0xPAIR2, CRO, 0xTOKEN"

# -------------------- LOG --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("main")

def tznow() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo(TZ))
    # Fallback χωρίς zoneinfo
    return datetime.now()

def mask(s: str, left: int = 6, right: int = 4) -> str:
    if not s or len(s) <= left + right:
        return s
    return s[:left] + "…" + s[-right:]

def parse_watchlist(s: str) -> list[str]:
    out: list[str] = []
    for part in (s or "").split(","):
        x = part.strip()
        if not x:
            continue
        # Δέξου μορφές τύπου "cronos/0x…" ή σκέτες διευθύνσεις/symbols
        if "/" in x:
            _, token = x.split("/", 1)
            token = token.strip()
            if token:
                out.append(token)
        else:
            out.append(x)
    # αφαίρεση διπλών, σταθερή σειρά
    dedup = []
    seen = set()
    for a in out:
        k = a.lower()
        if k not in seen:
            seen.add(k)
            dedup.append(a)
    return dedup

def fetch_prices(assets: list[str]) -> list[tuple[str, float]]:
    rows = []
    for a in assets:
        try:
            p = get_price_usd(a, chain="cronos")
        except Exception:
            p = 0.0
        rows.append((a, float(p or 0.0)))
    return rows

def build_status(assets: list[str]) -> str:
    now = tznow()
    wallet = mask(WALLET_ADDRESS) if WALLET_ADDRESS else "(no wallet)"
    prices = fetch_prices(assets)
    lines = []
    lines.append(f"{APP_NAME}")
    lines.append(f"Time: {now.strftime('%Y-%m-%d %H:%M:%S')} ({TZ})")
    lines.append(f"Wallet: {wallet}")
    if not assets:
        lines.append("Watchlist: (empty) — set DEX_PAIRS to see token prices")
    else:
        lines.append("Watchlist snapshot (USD):")
        for sym, price in prices:
            # σταθεροποίηση output (χωρίς ειδικούς MDV2 χαρακτήρες)
            lines.append(f"  - {sym}: {price:.8f}")
    # δείξε και μικρό HISTORY seed αν υπάρχει
    if HISTORY_LAST_PRICE:
        lines.append("Seed/Historical cache present.")
    return "\n".join(lines)

def send_startup_ping(assets: list[str]) -> None:
    text = build_status(assets)
    ok, status, resp = send_telegram(text)  # plain text, no parse_mode -> safe
    if not ok:
        log.warning("Telegram send failed %s: %s", status, resp)
    else:
        log.info("Startup ping sent to Telegram.")

def parse_eod_time(hhmm: str, base: datetime) -> datetime:
    try:
        hh, mm = [int(x) for x in hhmm.split(":")]
        candidate = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= base:
            candidate = candidate + timedelta(days=1)
        return candidate
    except Exception:
        # fallback 23:59
        candidate = base.replace(hour=23, minute=59, second=0, microsecond=0)
        if candidate <= base:
            candidate = candidate + timedelta(days=1)
        return candidate

def loop_runtime(assets: list[str], intraday_hours: float, eod_hhmm: str) -> None:
    log.info("Starting loop: intraday=%.2fh, EOD=%s", intraday_hours, eod_hhmm)
    last_intraday = tznow()
    next_intraday = last_intraday + timedelta(hours=max(intraday_hours, 0.5))
    next_eod = parse_eod_time(eod_hhmm, tznow())

    # send immediate startup
    send_startup_ping(assets)

    while True:
        now = tznow()

        # Intraday tick
        if now >= next_intraday:
            text = "🟡 Intraday Update\n" + build_status(assets)
            ok, status, resp = send_telegram(text)
            if not ok:
                log.warning("Telegram intraday failed %s: %s", status, resp)
            else:
                log.info("Intraday sent.")
            last_intraday = now
            next_intraday = now + timedelta(hours=max(intraday_hours, 0.5))

        # EOD tick
        if now >= next_eod:
            text = "🔵 EOD Report\n" + build_status(assets)
            ok, status, resp = send_telegram(text)
            if not ok:
                log.warning("Telegram EOD failed %s: %s", status, resp)
            else:
                log.info("EOD sent.")
            next_eod = parse_eod_time(eod_hhmm, tznow())

        time.sleep(10)  # lightweight polling

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--once", action="store_true", help="Run one-shot status and exit")
    parser.add_argument("--send-test", action="store_true", help="Send a Telegram test message and exit")
    args = parser.parse_args(argv)

    assets = parse_watchlist(DEX_PAIRS)
    log.info("Config | TZ=%s | INTRADAY_HOURS=%.2f | EOD_TIME=%s | DEX_PAIRS=%s",
             TZ, INTRADAY_HOURS, EOD_TIME, ", ".join(assets) or "(empty)")

    if args.send_test:
        ok, status, resp = send_telegram("Test message (plain).")
        log.info("Telegram test => ok=%s status=%s resp=%s", ok, status, resp[:200])
        return 0

    if args.once:
        text = build_status(assets)
        ok, status, resp = send_telegram(text)
        if not ok:
            log.warning("Telegram send failed %s: %s", status, resp)
        else:
            log.info("One-shot status sent.")
        # Επίσης γράψε στο stdout
        print("\n" + text + "\n")
        return 0

    # Default: run loop
    try:
        loop_runtime(assets, INTRADAY_HOURS, EOD_TIME)
    except KeyboardInterrupt:
        log.info("Interrupted. Bye.")
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
