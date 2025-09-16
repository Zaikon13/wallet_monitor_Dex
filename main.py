#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — lean main.py
- Startup ping στο Telegram
- Alerts scanning για DEX_PAIRS (Dexscreener)
- Intraday summary ανά INTRADAY_HOURS
- EOD summary στο EOD_TIME (τοπική ζώνη ώρας)
Απαιτούμενα env:
  TZ=Europe/Athens
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  DEX_PAIRS=cronos/0xPAIR1,cronos/0xPAIR2  (ή μόνο 0xPAIR...)
  INTRADAY_HOURS=3
  EOD_TIME=23:59
(Προαιρετικά)
  WALLET_ADDRESS=...
  RPC_URL=...
  ETHERSCAN_API=...
"""

from __future__ import annotations
import os, sys, time, signal, logging, threading
from datetime import datetime, timedelta

from core.config import apply_env_aliases, get_env
from core.tz import tz_init, now_dt, ymd
from telegram.api import send_telegram
from core.alerts import check_pair_alert
from core.pricing import get_price_usd, HISTORY_LAST_PRICE, seed_price

# -------------------- Logging --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("sentinel")

# -------------------- ENV / bootstrap --------------------
apply_env_aliases()
TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = tz_init(TZ)

INTRADAY_HOURS = float(os.getenv("INTRADAY_HOURS", "3"))  # π.χ. 3.0 ώρες
EOD_TIME = os.getenv("EOD_TIME", "23:59")                  # HH:MM (τοπική ώρα)

DEX_PAIRS_RAW = os.getenv("DEX_PAIRS", "")  # π.χ. "cronos/0xabc...,cronos/0xdef..."
# επιτρέπουμε και απλές διευθύνσεις χωρίς "cronos/"
def _parse_pairs(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in [x.strip() for x in raw.split(",") if x.strip()]:
        if "/" in chunk:
            chain, addr = chunk.split("/", 1)
            out.append((chain.strip().lower(), addr.strip().lower()))
        else:
            # default chain = cronos
            out.append(("cronos", chunk.strip().lower()))
    return out

DEX_PAIRS = _parse_pairs(DEX_PAIRS_RAW)

# seed προαιρετικών γνωστών τιμών (αν θέλεις)
try:
    seed_price("CRO", float(os.getenv("SEED_CRO_USD", "0.0")))
except Exception:
    pass

# -------------------- Helpers --------------------
_shutdown = threading.Event()

def _short(addr: str) -> str:
    return addr[:6] + "…" + addr[-4:] if len(addr) > 12 else addr

def _pair_label(chain: str, addr: str) -> str:
    return f"{chain}:{_short(addr)}"

def _prices_snapshot() -> list[str]:
    """Επιστρέφει γραμμές με τις τρέχουσες τιμές USD για τα DEX_PAIRS."""
    lines = []
    if not DEX_PAIRS:
        return ["No DEX_PAIRS configured."]
    for chain, addr in DEX_PAIRS:
        p = get_price_usd(addr, chain=chain)
        lines.append(f"• {_pair_label(chain, addr)}  ${p:,.6f}")
    return lines

def send_startup_ping():
    ts = now_dt(LOCAL_TZ)
    lines = [
        "✅ Sentinel started",
        f"TZ: {TZ} | Now: {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"INTRADAY_HOURS: {INTRADAY_HOURS} | EOD_TIME: {EOD_TIME}",
        f"DEX_PAIRS: {', '.join([f'{c}/{_short(a)}' for c,a in DEX_PAIRS]) or '—'}",
        "",
        "Prices snapshot:",
        *(_prices_snapshot()),
    ]
    ok, code, resp = send_telegram("\n".join(lines))  # plain text, ασφαλές
    if not ok:
        log.warning("Telegram send failed %s: %s", code, resp)
    else:
        log.info("Startup ping sent.")

# -------------------- Loops --------------------
def alerts_loop():
    log.info("Alerts loop: started (pairs=%d).", len(DEX_PAIRS))
    while not _shutdown.is_set():
        if not DEX_PAIRS:
            time.sleep(10)
            continue
        for chain, addr in DEX_PAIRS:
            try:
                msg = check_pair_alert(addr, chain=chain)
                if msg:
                    log.info("Alert sent: %s", msg)
            except Exception as e:
                log.warning("Alert check error for %s/%s: %s", chain, _short(addr), e)
        # scan κάθε 60s
        _shutdown.wait(60.0)

def intraday_loop():
    if INTRADAY_HOURS <= 0:
        log.info("Intraday loop disabled (INTRADAY_HOURS<=0).")
        return
    interval = max(0.2, INTRADAY_HOURS)  # ελάχιστο ~12'
    seconds = int(interval * 3600)
    log.info("Intraday loop: every %.2f hours (~%dm).", interval, seconds // 60)
    while not _shutdown.is_set():
        ts = now_dt(LOCAL_TZ)
        title = f"🟡 Intraday Update — {ts.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        lines = [title, "", *(_prices_snapshot())]
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
        # περίμενε μέχρι τότε ή μέχρι shutdown
        if _shutdown.wait(max(1.0, wait_sec)):
            break
        # ώρα για EOD
        title = f"🔵 EOD Summary — {ymd(nxt)}"
        lines = [title, "", *(_prices_snapshot())]
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

    # Startup ping
    try:
        send_startup_ping()
    except Exception as e:
        log.warning("Startup ping error: %s", e)

    # Threads
    threads: list[threading.Thread] = []

    t_alerts = threading.Thread(target=alerts_loop, name="alerts-loop", daemon=True)
    t_alerts.start(); threads.append(t_alerts)

    t_intraday = threading.Thread(target=intraday_loop, name="intraday-loop", daemon=True)
    t_intraday.start(); threads.append(t_intraday)

    t_eod = threading.Thread(target=eod_loop, name="eod-loop", daemon=True)
    t_eod.start(); threads.append(t_eod)

    # Κύριος βρόχος: απλά περιμένει μέχρι shutdown
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
