#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (compact)
- Keeps your existing orchestration (schedule, watcher, wallet_monitor, telegram_long_poll_loop).
- Adds robust diagnostics when holdings snapshot is empty (RPC block, CRO probe, key envs).
- Safer intraday cooldown & informative updates.
- Logs essential envs at startup.

Compatible with your current modules:
  core.*, reports.*, telegram.*, utils.*, etc.
"""

import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

import schedule
from dotenv import load_dotenv

from core.guards import set_holdings
from core.holdings import get_wallet_snapshot
from core.providers.cronos import fetch_wallet_txs  # used by wallet monitor factory
from core.runtime_state import note_tick, update_snapshot
from core.watch import make_from_env
from core.wallet_monitor import make_wallet_monitor
from reports.day_report import build_day_report_text
from reports.weekly import build_weekly_report_text
from reports.scheduler import run_pending
from telegram.api import send_telegram, send_telegram_messages, telegram_long_poll_loop
from telegram.dispatcher import dispatch

# Optional diagnostics (if helpers exist in your repo)
try:
    from core.providers.cronos import ping_block_number, get_native_balance, rpc_url  # type: ignore
except Exception:  # pragma: no cover
    def ping_block_number():
        return None
    def get_native_balance(_addr: str):
        return 0
    def rpc_url() -> str:
        return os.getenv("CRONOS_RPC_URL", "")

try:
    from core.signals.server import start_signals_server_if_enabled
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    def start_signals_server_if_enabled():
        logging.warning("signals server disabled: Flask missing")


_shutdown = False
_last_error_ts = float("-inf")
_last_intraday_signature: Optional[tuple] = None
_wallet_address: Optional[str] = None


# ---------------------------------------------------------------------------
# Env helpers & logging
# ---------------------------------------------------------------------------

def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    return value if value else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logging.debug("invalid float env %s=%r", name, raw)
        return default


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def _handle_shutdown(sig, frm):
    global _shutdown
    _shutdown = True


# ---------------------------------------------------------------------------
# Snapshot signature (to avoid spam when identical)
# ---------------------------------------------------------------------------

def _snapshot_signature(snapshot: dict) -> tuple:
    """Builds a hashable signature from snapshot payload values used by intraday."""
    items = []
    for symbol, payload in sorted(snapshot.items()):
        qty = payload.get("qty") or payload.get("amount") or "0"
        usd = (
            payload.get("usd")
            or payload.get("value_usd")
            or payload.get("usd_value")
            or "0"
        )
        items.append((symbol, str(qty), str(usd)))
    return tuple(items)


# ---------------------------------------------------------------------------
# Diagnostics helpers
# ---------------------------------------------------------------------------

def _diagnostics_empty_snapshot(addr: str) -> str:
    lines = ["🧾 Holdings", "❌ Empty snapshot", ""]
    lines.append(f"• WALLET_ADDRESS: {addr or '(missing)'}")
    lines.append(f"• CRONOS_RPC_URL: {rpc_url() or '(missing)'}")
    try:
        bn = ping_block_number()
        lines.append(f"• RPC block: {bn if bn is not None else '(no response)'}")
    except Exception:
        lines.append("• RPC block: (error)")
    try:
        cro = get_native_balance(addr) if addr else 0
        lines.append(f"• CRO balance probe: {cro}")
    except Exception:
        lines.append("• CRO balance probe: (error)")
    lines.append("")
    lines.append("Tip: set TOKENS_ADDRS / TOKENS_DECIMALS for ERC-20 balances.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report senders
# ---------------------------------------------------------------------------

def _send_daily_report() -> None:
    try:
        snapshot = get_wallet_snapshot(_wallet_address)
        update_snapshot(snapshot, time.time())
        report = build_day_report_text(
            intraday=False,
            wallet=_wallet_address,
            snapshot=snapshot,
        )
    except Exception as exc:
        logging.exception("daily report generation failed: %s", exc)
        send_telegram("⚠️ Failed to generate daily report.", dedupe=False)
        return
    send_telegram_messages([report])


def _send_weekly_report(days: int = 7) -> None:
    try:
        report = build_weekly_report_text(days=days, wallet=_wallet_address)
    except Exception as exc:
        logging.exception("weekly report generation failed: %s", exc)
        send_telegram("⚠️ Failed to generate weekly report.", dedupe=False)
        return
    send_telegram_messages([report])


def _send_health_ping() -> None:
    send_telegram("✅ alive", dedupe=False)


# ---------------------------------------------------------------------------
# Intraday update (resilient + diagnostics for empty)
# ---------------------------------------------------------------------------

def _send_intraday_update() -> None:
    global _last_intraday_signature
    try:
        snapshot = get_wallet_snapshot(_wallet_address) or {}
        update_snapshot(snapshot, time.time())
        signature = _snapshot_signature(snapshot)
    except Exception as exc:
        logging.exception("intraday snapshot failed: %s", exc)
        send_telegram("⚠️ Failed to refresh snapshot.", dedupe=False)
        return

    if not snapshot:
        # Emit diagnostics instead of a bare "Empty snapshot"
        send_telegram_messages([_diagnostics_empty_snapshot(_wallet_address or "")])
        return

    if signature and signature == _last_intraday_signature:
        # Gentle cooldown (non-spam). If you prefer silence, just `return`.
        send_telegram("⌛ cooldown")
        return

    _last_intraday_signature = signature

    total_usd = 0.0
    top_symbol = None
    top_value = 0.0
    for symbol, payload in snapshot.items():
        try:
            usd = float(
                payload.get("usd")
                or payload.get("value_usd")
                or payload.get("usd_value")
                or 0.0
            )
        except (TypeError, ValueError):
            usd = 0.0
        total_usd += usd
        if usd > top_value:
            top_symbol = symbol
            top_value = usd

    lines = ["🕒 Intraday Update"]
    lines.append(f"Assets: {len(snapshot)} | Total ≈ ${total_usd:,.2f}")
    if top_symbol:
        lines.append(f"Top: {top_symbol.upper()} ≈ ${top_value:,.2f}")
    send_telegram_messages(["\n".join(lines)])


# ---------------------------------------------------------------------------
# Weekly schedule helper
# ---------------------------------------------------------------------------

def _schedule_weekly_job(dow: str, at_time: str) -> None:
    mapper = {
        "MON": schedule.every().monday,
        "TUE": schedule.every().tuesday,
        "WED": schedule.every().wednesday,
        "THU": schedule.every().thursday,
        "FRI": schedule.every().friday,
        "SAT": schedule.every().saturday,
        "SUN": schedule.every().sunday,
    }
    job = mapper.get(dow.upper()) if dow else None
    if not job:
        logging.warning("invalid WEEKLY_DOW %s", dow)
        return
    try:
        job.at(at_time).do(_send_weekly_report)
    except schedule.ScheduleValueError as exc:
        logging.warning("weekly schedule error: %s", exc)


# ---------------------------------------------------------------------------
# Telegram long-poll thread (existing implementation hook)
# ---------------------------------------------------------------------------

def _start_telegram_thread() -> None:
    if not callable(telegram_long_poll_loop):
        return
    tg_thread = threading.Thread(target=telegram_long_poll_loop, args=(dispatch,), daemon=True)
    tg_thread.start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _wallet_address
    load_dotenv()
    _setup_logging()

    # Log essential envs on startup (so we can see misconfig quickly)
    _wallet_address = _env_str("WALLET_ADDRESS", "")
    logging.info("WALLET_ADDRESS=%s", _wallet_address or "(missing)")
    logging.info("CRONOS_RPC_URL=%s", rpc_url() or "(missing)")

    send_telegram("✅ Cronos DeFi Sentinel started and is online.")

    # EOD
    eod_time = _env_str("EOD_TIME", "23:59")
    try:
        schedule.every().day.at(eod_time).do(_send_daily_report)
    except schedule.ScheduleValueError as exc:
        logging.warning("invalid EOD_TIME %s: %s", eod_time, exc)
    else:
        logging.info("daily report scheduled at %s", eod_time)

    # Weekly
    weekly_dow = _env_str("WEEKLY_DOW", "SUN")
    weekly_time = _env_str("WEEKLY_TIME", "18:00")
    _schedule_weekly_job(weekly_dow, weekly_time)

    # Health ping
    health_min = _env_float("HEALTH_MIN", 30.0)
    if health_min > 0:
        interval = max(1, int(health_min))
        try:
            schedule.every(interval).minutes.do(_send_health_ping)
        except schedule.ScheduleValueError as exc:
            logging.warning("health ping schedule error: %s", exc)
        else:
            logging.info("health ping scheduled every %s minute(s)", interval)
    else:
        logging.info("health ping disabled")

    # Intraday
    intraday_hours = _env_float("INTRADAY_HOURS", 1.0)
    if intraday_hours > 0:
        try:
            schedule.every(max(1, int(intraday_hours))).hours.do(_send_intraday_update)
        except schedule.ScheduleValueError as exc:
            logging.warning("intraday schedule error: %s", exc)
    else:
        logging.info("intraday updates disabled")

    # Watchers
    watcher = make_from_env()
    wallet_mon = make_wallet_monitor(provider=fetch_wallet_txs)

    # Signals server (optional)
    try:
        start_signals_server_if_enabled()
    except Exception as exc:
        logging.warning("signals server error: %s", exc)

    # Shutdown signals
    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # Periodic holdings refresh for guards
    holdings_refresh = int(os.getenv("HOLDINGS_REFRESH_SEC", "60") or 60)
    last_hold = 0.0

    # Start Telegram polling
    _start_telegram_thread()

    # Main loop
    while not _shutdown:
        t0 = time.time()
        try:
            watcher.poll_once()
            wallet_mon.poll_once()
            note_tick()
            run_pending()

            # Update holdings/state periodically
            if time.time() - last_hold >= holdings_refresh:
                snapshot = get_wallet_snapshot(_wallet_address) or {}
                update_snapshot(snapshot, time.time())
                set_holdings(set(snapshot.keys()))
                # If empty, emit diagnostics once per refresh interval
                if not snapshot:
                    send_telegram_messages([_diagnostics_empty_snapshot(_wallet_address or "")])
                last_hold = time.time()

        except Exception as exc:
            logging.exception("loop error: %s", exc)
            now = time.monotonic()
            global _last_error_ts
            if now - _last_error_ts >= 120:
                send_telegram("⚠️ runtime error (throttled)", dedupe=False)
                _last_error_ts = now

        # keep cadence stable
        sleep_for = max(0.5, _env_float("WALLET_POLL", 15.0) - (time.time() - t0))
        time.sleep(sleep_for)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logging.exception("fatal: %s", exc)
        try:
            send_telegram(f"💥 Fatal error: {exc}")
        except Exception:
            pass
        sys.exit(1)
