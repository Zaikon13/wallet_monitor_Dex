#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py (stabilized v6)
Cronos DeFi Sentinel — lean runtime
- Watchlist scanner (uses core/watch.py if present)
- Intraday & EOD holdings reports (RPC + Dexscreener)
- Keeps <1000 lines; minimal deps
"""
from __future__ import annotations
import os, sys, time, signal, logging, threading
from datetime import datetime

from core.config import apply_env_aliases
from core.tz import tz_init, now_dt, ymd
from telegram.api import send_telegram

# Optional imports guarded (so missing modules don't crash)
try:
    from core.watch import scan_watchlist, load_watchlist
except Exception:
    scan_watchlist = None  # type: ignore
    def load_watchlist(_path: str):
        return []

# --- ENV/bootstrap ---
apply_env_aliases()
DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = tz_init(TZ)

# Timers
INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE = int(os.getenv("EOD_MINUTE", "59"))
WATCH_POLL = int(os.getenv("WATCH_POLL", os.getenv("DISCOVER_POLL", "120")))

WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("sentinel.v6")

shutdown_event = threading.Event()

# ===================== Helpers =====================

def _format_amt(x: float) -> str:
    try:
        x = float(x)
    except Exception:
        return str(x)
    if abs(x) >= 1:
        return f"{x:,.4f}"
    if abs(x) >= 1e-4:
        return f"{x:.6f}"
    return f"{x:.8f}"

# ===================== Watchlist =====================

def watchlist_loop():
    if not scan_watchlist:
        log.info("watchlist disabled (core/watch.py not present)")
        return
    wl = load_watchlist(WATCHLIST_PATH)
    send_telegram(f"🔍 Watchlist scanner ON ({len(wl)} entries)")
    while not shutdown_event.is_set():
        try:
            alerts = scan_watchlist(wl)
            if alerts:
                try:
                    from telegram.api import send_watchlist_alerts
                    send_watchlist_alerts(alerts)
                except Exception:
                    # fallback: plain message
                    lines = ["*🔍 Watchlist Alerts*"]
                    for a in alerts:
                        lines.append(str(a))
                    send_telegram("\n".join(lines))
        except Exception as e:
            log.warning("watchlist loop error: %s", e)
        for _ in range(WATCH_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ===================== Reports =====================

def _compose_holdings_text() -> str:
    try:
        from core.holdings import compute_holdings
        tot, br = compute_holdings()
        if not br:
            return "📦 No assets detected yet."
        lines = ["*💼 Holdings (MTM)*", f"Σύνολο: ${tot:,.2f}"]
        for b in br[:18]:
            px = b.get("price_usd") or 0
            val = b.get("usd_value") or 0
            lines.append(
                f"• {b['token']}: {_format_amt(b['amount'])} @ ${px:.6f} = ${val:,.2f}"
            )
        if len(br) > 18:
            lines.append(f"… και {len(br)-18} ακόμη")
        return "\n".join(lines)
    except Exception as e:
        log.warning("holdings text error: %s", e)
        return "(holdings unavailable)"


def intraday_report_loop():
    send_telegram("⏱ Intraday reporting enabled.")
    while not shutdown_event.is_set():
        try:
            send_telegram(_compose_holdings_text())
        except Exception as e:
            log.warning("intraday loop error: %s", e)
        # sleep INTRADAY_HOURS
        for _ in range(max(1, INTRADAY_HOURS) * 3600 // 15):
            if shutdown_event.is_set():
                break
            time.sleep(15)


def eod_report_loop():
    send_telegram(f"🕛 EOD scheduler active: {EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ}")
    last_sent = ""
    while not shutdown_event.is_set():
        now = now_dt()
        if now.strftime("%H:%M") == f"{EOD_HOUR:02d}:{EOD_MINUTE:02d}" and last_sent != ymd(now):
            try:
                txt = _compose_holdings_text()
                send_telegram("🟢 *End of Day*\n" + txt)
                last_sent = ymd(now)
            except Exception as e:
                log.warning("EOD error: %s", e)
        time.sleep(20)

# ===================== Entrypoint =====================

def main():
    send_telegram("✅ Sentinel v6 started.")
    threads = [
        threading.Thread(target=watchlist_loop, name="watchlist", daemon=True),
        threading.Thread(target=intraday_report_loop, name="intraday", daemon=True),
        threading.Thread(target=eod_report_loop, name="eod", daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        while not shutdown_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown_event.set()
    for t in threads:
        t.join(timeout=5)


def _graceful_exit(*_):
    shutdown_event.set()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    main()
