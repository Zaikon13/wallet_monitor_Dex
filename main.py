#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py
- Startup ping στο Telegram (με prices + wallet snapshot)
- Alerts scanning για DEX_PAIRS (Dexscreener)
- Intraday summary ανά INTRADAY_HOURS (με wallet snapshot)
- EOD summary στο EOD_TIME (με wallet snapshot)

Απαιτούμενα env:
  TZ=Europe/Athens
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  WALLET_ADDRESS=0x...
  RPC_URL=https://cronos-evm-rpc.publicnode.com
  DEX_PAIRS=cronos/0x...,cronos/0x...

Προαιρετικά:
  INTRADAY_HOURS=3
  EOD_TIME=23:59
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

# -------------------- Logging --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("sentinel")

# -------------------- ENV / bootstrap --------------------
apply_env_aliases()

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = tz_init(TZ)

def _parse_pairs(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in [x.strip() for x in (raw or "").split(",") if x.strip()]:
        if "/" in chunk:
            chain, addr = chunk.split("/", 1)
            out.append((chain.strip().lower(), addr.strip().lower()))
        else:
            out.append(("cronos", chunk.strip().lower()))
    return out

DEX_PAIRS = _parse_pairs(os.getenv("DEX_PAIRS", ""))

def _short(addr: str) -> str:
    return addr[:6] + "…" + addr[-4:] if len(addr) > 12 else addr

def _pair_label(chain: str, addr: str) -> str:
    return f"{chain}:{_short(addr)}"

def _prices_snapshot() -> list[str]:
    lines = []
    if not DEX_PAIRS:
        return ["No DEX_PAIRS configured."]
    for chain, addr in DEX_PAIRS:
        p = get_price_usd(addr, chain=chain)
        lines.append(f"• {_pair_label(chain, addr)}  ${p:,.6f}")
    return lines

# -------------------- Wallet snapshot helpers --------------------
def _wallet_snapshot_lines() -> list[str]:
    try:
        snap = get_wallet_snapshot()
        lines = ["Wallet snapshot:"]
        lines.extend(format_snapshot_lines(snap))
        return lines
    except Exception as e:
        log.warning("Wallet snapshot error: %s", e)
        return [f"Wallet snapshot: ERROR -> {e}"]

# -------------------- Startup ping --------------------
def send_startup_ping():
    ts = now_dt(LOCAL_TZ)
    lines = [
        "✅ Sentinel started",
        f"TZ: {TZ} | Now: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"DEX_PAIRS: {', '.join([f'{c}/{_short(a)}' for c,a in DEX_PAIRS]) or '—'}",
        "",
        "Prices snapshot:",
        *(_prices_snapshot()),
        "",
        *(_wallet_snapshot_lines()),
    ]
    text = "\n".join(lines)
    ok, code, resp = send_telegram(text)  # plain text
    if not ok:
        log.warning("Telegram send failed %s: %s", code, resp)
    else:
        log.info("Startup ping sent (with wallet snapshot).")

# -------------------- Loops --------------------
_shutdown = threading.Event()
INTRADAY_HOURS = float(os.getenv("INTRADAY_HOURS", "3"))
EOD_TIME = os.getenv("EOD_TIME", "23:59")

def alerts_loop():
    log.info("Alerts loop: started (pairs=%d).", len(DEX_PAIRS))
    while not _shutdown.is_set():
        if not DEX_PAIRS:
            _shutdown.wait(10.0); continue
        for chain, addr in DEX_PAIRS:
            try:
                msg = check_pair_alert(addr, chain=chain)
                if msg:
                    log.info("Alert sent: %s", msg)
            except Exception as e:
                log.warning("Alert check error for %s/%s: %s", chain, _short(addr), e)
        _shutdown.wait(60.0)

def intraday_loop():
    if INTRADAY_HOURS <= 0:
        log.info("Intraday loop disabled (INTRADAY_HOURS<=0).")
        return
    interval = max(0.2, INTRADAY_HOURS)  # min ~12'
    seconds = int(interval * 3600)
    log.info("Intraday loop: every %.2f hours (~%dm).", interval, seconds // 60)
    while not _shutdown.is_set():
        ts = now_dt(LOCAL_TZ)
        lines = [f"🟡 Intraday Update — {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}", ""]
        lines.extend(_prices_snapshot())
        lines.append("")
        lines.extend(_wallet_snapshot_lines())
        ok, code, resp = send_telegram("\n".join(lines))
        if not ok:
            log.warning("Telegram intraday failed %s: %s", code, resp)
        _shutdown.wait(seconds)

def _next_eod_dt(now_local: datetime) -> datetime:
    try:
        hh, mm = [int(x) for x in EOD_TIME.split(":")]
    except Exception:
        hh, mm = 23, 59
    candidate = now_local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now_local:
        candidate = candidate + timedelta(days=1)
    return candidate

def eod_loop():
    log.info("EOD loop: target time daily at %s (%s).", EOD_TIME, TZ)
    while not _shutdown.is_set():
        now_local = now_dt(LOCAL_TZ)
        nxt = _next_eod_dt(now_local)
        wait_sec = (nxt - now_local).total_seconds()
        log.info("EOD next at %s (in %d min).", nxt.strftime("%Y-%m-%d %H:%M:%S"), int(wait_sec // 60))
        if _shutdown.wait(max(1.0, wait_sec)):
            break
        lines = [f"🔵 EOD Summary — {ymd(nxt)}", ""]
        lines.extend(_prices_snapshot())
        lines.append("")
        lines.extend(_wallet_snapshot_lines())
        ok, code, resp = send_telegram("\n".join(lines))
        if not ok:
            log.warning("Telegram EOD failed %s: %s", code, resp)

# -------------------- Main --------------------
def _install_signals():
    def _sigterm(_signo, _frame):
        log.info("SIGTERM received — shutting down…")
        _shutdown.set()
    signal.signal(signal.SIGINT, _sigterm)
    signal.signal(signal.SIGTERM, _sigterm)

def main() -> int:
    log.info("Starting Sentinel…")
    _install_signals()
    # sanity log για env που χρειαζόμαστε
    for k in ("WALLET_ADDRESS","RPC_URL","CRONOS_RPC_URL","DEX_PAIRS"):
        v = os.getenv(k)
        log.info("ENV %s: %s", k, ("SET" if v else "MISSING"))
    try:
        send_startup_ping()
    except Exception as e:
        log.warning("Startup ping error: %s", e)

    threads: list[threading.Thread] = []
    t_alerts = threading.Thread(target=alerts_loop, name="alerts-loop", daemon=True); t_alerts.start(); threads.append(t_alerts)
    t_intraday = threading.Thread(target=intraday_loop, name="intraday-loop", daemon=True); t_intraday.start(); threads.append(t_intraday)
    t_eod = threading.Thread(target=eod_loop, name="eod-loop", daemon=True); t_eod.start(); threads.append(t_eod)

    try:
        while not _shutdown.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        _shutdown.set()

    log.info("Waiting threads to finish…")
    for t in threads:
        t.join(timeout=5.0)
    log.info("Sentinel exited cleanly.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
