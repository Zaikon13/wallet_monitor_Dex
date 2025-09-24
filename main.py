#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# main.py — rescue boot + optional holdings snapshot (safe)
from __future__ import annotations
import os, time, logging, signal
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any
import requests
from dotenv import load_dotenv
import schedule

# ---------------- Logging ----------------
def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

# ---------------- Env helpers ----------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None: return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def get_eod_time() -> str:
    t = os.getenv("EOD_TIME", "23:59").strip()
    try:
        hh, mm = t.split(":"); ih, im = int(hh), int(mm)
        if 0 <= ih <= 23 and 0 <= im <= 59:
            return f"{ih:02d}:{im:02d}"
    except Exception:
        pass
    logging.warning("Invalid EOD_TIME '%s' → using 23:59", t)
    return "23:59"

def get_intraday_hours() -> Optional[int]:
    v = os.getenv("INTRADAY_HOURS", "").strip()
    if not v: return None
    try:
        iv = int(v)
        return iv if iv > 0 else None
    except Exception:
        logging.warning("Invalid INTRADAY_HOURS '%s' (ignored)", v)
        return None

# ---------------- Telegram (plain text) ----------------
def send_telegram(text: str) -> None:
    bot = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        logging.info("Telegram not configured (TELEGRAM_BOT_TOKEN/CHAT_ID missing)")
        return
    url = f"https://api.telegram.org/bot{bot}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        if r.status_code != 200:
            logging.warning("Telegram send failed %s: %s", r.status_code, r.text)
    except Exception as e:
        logging.exception("Telegram send error: %s", e)

# ---------------- Optional: holdings snapshot ----------------
def _D(x: Any) -> Decimal:
    try: return Decimal(str(x))
    except Exception: return Decimal("0")

def _format_holdings_plain(snapshot: Dict[str, Dict[str, Any]]) -> str:
    if not snapshot: return "Empty snapshot."
    lines = ["Holdings Snapshot", ""]
    total = Decimal("0")
    for sym, item in snapshot.items():
        qty = _D(item.get("amount", item.get("qty", 0)))
        price = _D(item.get("price", item.get("price_usd", 0)))
        usd = qty * price
        total += usd
        lines.append(f"{sym:<8} {qty:>12,.4f} x ${price:,.6f} = ${usd:,.2f}")
    lines.append("")
    lines.append(f"Total: ${total:,.2f}")
    return "\n".join(lines)

def job_holdings_snapshot() -> None:
    """
    Tries to use project modules if present:
      - core.holdings.get_wallet_snapshot([address])
      - telegram.formatters.format_holdings(snapshot)
    Falls back to a plain-text formatter. Never crashes main loop.
    """
    try:
        # Lazy imports so rescue boot never breaks
        try:
            from core.holdings import get_wallet_snapshot  # type: ignore
        except Exception as e:
            logging.info("holdings: module not available (%s)", e)
            get_wallet_snapshot = None  # type: ignore

        try:
            from telegram.formatters import format_holdings  # type: ignore
        except Exception:
            format_holdings = None  # type: ignore

        if not get_wallet_snapshot:
            send_telegram("🧾 Holdings: module not available.")
            return

        wallet = os.getenv("WALLET_ADDRESS", "").strip()
        try:
            snap = get_wallet_snapshot(wallet) if wallet else get_wallet_snapshot()  # type: ignore
        except TypeError:
            snap = get_wallet_snapshot()  # type: ignore

        if format_holdings:
            try:
                text = format_holdings(snap)  # type: ignore
                # strip any markdown to stay plain
                send_telegram(f"🧾 Holdings\n{text}")
                return
            except Exception as e:
                logging.warning("format_holdings failed, fallback to plain: %s", e)

        # Fallback plain-text
        send_telegram("🧾 " + _format_holdings_plain(snap or {}))
    except Exception as e:
        logging.exception("job_holdings_snapshot failed: %s", e)
        try: send_telegram("⚠️ Holdings snapshot failed.")
        except Exception: pass

# ---------------- Jobs ----------------
def job_startup() -> None:
    send_telegram("✅ Sentinel started (rescue+holdings).")

def job_daily_report() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    msg = f"📒 Daily Report — {today}\nStatus: OK"
    send_telegram(msg)

# ---------------- Loop ----------------
def bind_schedules() -> None:
    schedule.every().day.at(get_eod_time()).do(job_daily_report)
    logging.info("Scheduled daily report at %s", get_eod_time())
    ih = get_intraday_hours()
    if ih:
        schedule.every(ih).hours.do(job_holdings_snapshot)
        logging.info("Scheduled holdings snapshot every %s hour(s)", ih)

def run_loop(stop: list[bool]) -> None:
    while not stop[0]:
        try:
            schedule.run_pending()
        except Exception as e:
            logging.exception("schedule.run_pending failed: %s", e)
        time.sleep(1)

def install_signals(stop: list[bool]) -> None:
    def _grace(sig, frame):
        logging.info("Signal %s — stopping...", sig)
        stop[0] = True
        try: send_telegram("🛑 Sentinel stopping...")
        except Exception: pass
    try:
        signal.signal(signal.SIGINT, _grace)
        signal.signal(signal.SIGTERM, _grace)
    except Exception:
        pass

# ---------------- Main ----------------
def main() -> int:
    setup_logging()
    try: load_dotenv(override=False)
    except Exception: pass

    logging.info("Starting Sentinel (rescue+holdings)...")
    job_startup()

    # Optional: one-time holdings snapshot on boot
    if env_bool("STARTUP_SNAPSHOT", True):
        job_holdings_snapshot()

    bind_schedules()
    stop = [False]
    install_signals(stop)
    try:
        run_loop(stop)
    except Exception as e:
        logging.exception("Fatal in main loop: %s", e)
        try: send_telegram("❌ Fatal error in main loop.")
        except Exception: pass
        return 1
    logging.info("Exit.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
