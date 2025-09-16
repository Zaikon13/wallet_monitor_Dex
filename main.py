#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (green-safe)
- Startup ping στο Telegram (με prices + wallet snapshot)
- Alerts loop (Dexscreener)
- Intraday summary με snapshot
- EOD summary με snapshot
"""

from __future__ import annotations
import os, sys, time, signal, logging, threading
from datetime import datetime, timedelta

from core.config import apply_env_aliases
from core.tz import tz_init, now_dt, ymd
from telegram.api import send_telegram
from core.alerts import check_pair_alert
from core.pricing import get_price_usd
from core.holdings import get_wallet_snapshot, format_snapshot_lines

# Logging
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("sentinel")

# ENV
apply_env_aliases()
TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = tz_init(TZ)
INTRADAY_HOURS = float(os.getenv("INTRADAY_HOURS", "3"))
EOD_TIME = os.getenv("EOD_TIME", "23:59")

# Parse DEX_PAIRS
def _parse_pairs(raw: str) -> list[tuple[str, str]]:
    out = []
    for chunk in [x.strip() for x in (raw or "").split(",") if x.strip()]:
        if "/" in chunk:
            chain, addr = chunk.split("/", 1)
            out.append((chain.strip().lower(), addr.strip().lower()))
        else:
            out.append(("cronos", chunk.strip().lower()))
    return out

DEX_PAIRS = _parse_pairs(os.getenv("DEX_PAIRS", ""))

def _short(addr: str) -> str:
    return addr[:6] + "…" + addr[-4:]

def _prices_snapshot() -> list[str]:
    lines = []
    for chain, addr in DEX_PAIRS:
        try:
            p = get_price_usd(addr, chain=chain)
            lines.append(f"• {chain}:{_short(addr)}  ${p:,.6f}")
        except Exception as e:
            lines.append(f"• {chain}:{_short(addr)} ERROR {e}")
    return lines or ["No DEX_PAIRS configured."]

def _wallet_snapshot_lines() -> list[str]:
    try:
        snap = get_wallet_snapshot()
        return format_snapshot_lines(snap)
    except Exception as e:
        return [f"Wallet snapshot error: {e}"]

# Startup
def send_startup_ping():
    ts = now_dt(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        "✅ Sentinel started",
        f"TZ={TZ} | Now={ts}",
        "",
        "Prices snapshot:",
        *(_prices_snapshot()),
        "",
        "Wallet snapshot:",
        *(_wallet_snapshot_lines()),
    ]
    ok, code, resp = send_telegram("\n".join(lines))
    log.info("Startup ping sent." if ok else f"Startup ping failed {code}: {resp}")

# Loops
_shutdown = threading.Event()

def alerts_loop():
    while not _shutdown.is_set():
        for chain, addr in DEX_PAIRS:
            try:
                msg = check_pair_alert(addr, chain=chain)
                if msg:
                    log.info("Alert: %s", msg)
            except Exception as e:
                log.warning("Alert check error: %s", e)
        _shutdown.wait(60)

def intraday_loop():
    if INTRADAY_HOURS <= 0: return
    while not _shutdown.is_set():
        ts = now_dt(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        lines = [f"🟡 Intraday Update — {ts}", ""]
        lines.extend(_prices_snapshot())
        lines.append("")
        lines.extend(_wallet_snapshot_lines())
        send_telegram("\n".join(lines))
        _shutdown.wait(int(INTRADAY_HOURS * 3600))

def eod_loop():
    while not _shutdown.is_set():
        now = now_dt(LOCAL_TZ)
        hh, mm = [int(x) for x in EOD_TIME.split(":")]
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now: target += timedelta(days=1)
        wait = (target - now).total_seconds()
        if _shutdown.wait(wait): break
        lines = [f"🔵 EOD Summary — {ymd(target)}", ""]
        lines.extend(_prices_snapshot())
        lines.append("")
        lines.extend(_wallet_snapshot_lines())
        send_telegram("\n".join(lines))

# Main
def main():
    log.info("Starting Sentinel…")
    send_startup_ping()
    threading.Thread(target=alerts_loop, daemon=True).start()
    threading.Thread(target=intraday_loop, daemon=True).start()
    threading.Thread(target=eod_loop, daemon=True).start()
    try:
        while not _shutdown.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        _shutdown.set()

if __name__ == "__main__":
    sys.exit(main())
