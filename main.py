#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (stable)
- Works with telegram/dispatcher.py (dispatch(text, chat_id)) & telegram/commands.py
- Safe parsing of holdings snapshot (qty/amount, usd/value_usd/usd_value, price/price_usd)
- No unterminated strings; ASCII quotes only
- Includes diagnostics on empty snapshot and throttled alerting
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

import schedule
from dotenv import load_dotenv

# ---- Core imports ----
from core.guards import set_holdings
from core.holdings import get_wallet_snapshot
from core.providers.cronos import fetch_wallet_txs
from core.runtime_state import note_tick, update_snapshot
from core.watch import make_from_env
from core.wallet_monitor import make_wallet_monitor
from reports.day_report import build_day_report_text
from reports.weekly import build_weekly_report_text
from reports.scheduler import run_pending
from telegram.api import send_telegram, send_telegram_messages, telegram_long_poll_loop
from telegram.dispatcher import dispatch

# Optional alerts wiring
try:
    from core.alerts import notify_error, notify_alert  # type: ignore
except Exception:
    def notify_error(context: str, err: Exception) -> None:
        logging.error("%s: %s", context, err)
    def notify_alert(text: str) -> None:
        logging.warning(text)

# Optional diagnostics helpers
try:
    from core.providers.cronos import ping_block_number, get_native_balance, rpc_url  # type: ignore
except Exception:
    def ping_block_number():
        return None
    def get_native_balance(_addr: str):
        return 0
    def rpc_url() -> str:
        return os.getenv("CRONOS_RPC_URL", "")

# Optional signals server
try:
    from core.signals.server import start_signals_server_if_enabled
except ModuleNotFoundError:
    def start_signals_server_if_enabled() -> None:
        logging.warning("signals server disabled: Flask missing")


_shutdown = False
_last_error_ts = float("-inf")
_last_intraday_signature: Optional[tuple] = None
_wallet_address: Optional[str] = None


# ---------------------------------------------------------------------------
# Env & logging
# ---------------------------------------------------------------------------

def _env_str(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    v = raw.strip()
    return v if v else default


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

def _handle_shutdown(sig, frm) -> None:
    global _shutdown
    _shutdown = True
    try:
        notify_alert("Sentinel stopping...")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Safe numeric parsing for mixed payload shapes
# ---------------------------------------------------------------------------

def _to_dec(x) -> Decimal:
    if x is None:
        return Decimal("0")
    if isinstance(x, (int, float, Decimal)):
        try:
            return Decimal(str(x))
        except Exception:
            return Decimal("0")
    s = str(x).strip()
    if not s or s == "?":
        return Decimal("0")
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _payload_qty_usd(p: dict) -> Tuple[Decimal, Decimal]:
    """Accepts payloads with keys: qty/amount, usd/value_usd/usd_value, price/price_usd."""
    if not isinstance(p, dict):
        return Decimal("0"), Decimal("0")
    qty = _to_dec(p.get("amount", p.get("qty", 0)))
    usd = _to_dec(p.get("usd") or p.get("value_usd") or p.get("usd_value"))
    if usd == 0:
        px = _to_dec(p.get("price") or p.get("price_usd"))
        if px != 0 and qty != 0:
            try:
                usd = (qty * px).quantize(Decimal("0.0001"))
            except Exception:
                usd = qty * px
    return qty, usd


def _snapshot_signature(snapshot: dict) -> tuple:
    items = []
    for sym, payload in sorted(snapshot.items()):
        q, u = _payload_qty_usd(payload)
        items.append((sym, str(q), str(u)))
    return tuple(items)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------

def _diagnostics_empty_snapshot(addr: str) -> str:
    lines = ["Holdings", "Empty snapshot", ""]
    lines.append("• WALLET_ADDRESS: " + (addr or "(missing)"))
    lines.append("• CRONOS_RPC_URL: " + (rpc_url() or "(missing)"))
    try:
        bn = ping_block_number()
        lines.append("• RPC block: " + (str(bn) if bn is not None else "(no response)"))
    except Exception:
        lines.append("• RPC block: (error)")
    try:
        cro = get_native_balance(addr) if addr else 0
        lines.append("• CRO balance probe: " + str(cro))
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
        snapshot = get_wallet_snapshot(_wallet_address) or {}
        update_snapshot(snapshot, time.time())
        report = build_day_report_text(
            intraday=False,
            wallet=_wallet_address,
            snapshot=snapshot,
        )
        send_telegram_messages([report])
    except Exception as exc:
        notify_error("daily report generation", exc)


def _send_weekly_report(days: int = 7) -> None:
    try:
        report = build_weekly_report_text(days=days, wallet=_wallet_address)
        send_telegram_messages([report])
    except Exception as exc:
        notify_error("weekly report generation", exc)


def _send_health_ping() -> None:
    try:
        send_telegram("alive", dedupe=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Intraday
# ---------------------------------------------------------------------------

def _send_intraday_update() -> None:
    global _last_intraday_signature
    try:
        snapshot = get_wallet_snapshot(_wallet_address) or {}
        update_snapshot(snapshot, time.time())
        signature = _snapshot_signature(snapshot)
    except Exception as exc:
        notify_error("intraday snapshot", exc)
        return

    if not snapshot:
        try:
            send_telegram(_diagnostics_empty_snapshot(_wallet_address or ""), dedupe=False)
        except Exception:
            pass
        return

    if signature and signature == _last_intraday_signature:
        try:
            send_telegram("cooldown")
        except Exception:
            pass
        return

    _last_intraday_signature = signature

    total_usd = Decimal("0")
    top_symbol = None
    top_value = Decimal("0")
    for symbol, payload in snapshot.items():
        _, usd = _payload_qty_usd(payload)
        total_usd += usd
        if usd > top_value:
            top_symbol, top_value = symbol, usd

    try:
        total_str = f"{float(total_usd):,.2f}"
    except Exception:
        total_str = str(total_usd)

    lines = [
        "Intraday Update",
        f"Assets: {len(snapshot)} | Total ≈ ${total_str}",
    ]
    if top_symbol:
        try:
            top_str = f"{float(top_value):,.2f}"
        except Exception:
            top_str = str(top_value)
        lines.append(f"Top: {top_symbol.upper()} ≈ ${top_str}")
    try:
        send_telegram_messages(["
".join(lines)])
    except Exception:
        pass


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
# Telegram long-poll thread
# ---------------------------------------------------------------------------

def _start_telegram_thread() -> None:
    if not callable(telegram_long_poll_loop):
        return
    threading.Thread(target=telegram_long_poll_loop, args=(dispatch,), daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    global _wallet_address
    load_dotenv()
    _setup_logging()

    _wallet_address = _env_str("WALLET_ADDRESS", "")
    logging.info("WALLET_ADDRESS=%s", _wallet_address or "(missing)")
    logging.info("CRONOS_RPC_URL=%s", rpc_url() or "(missing)")

    try:
        send_telegram("Cronos DeFi Sentinel started and is online.")
    except Exception as exc:
        logging.debug("startup telegram failed: %s", exc, exc_info=True)

    # EOD
    eod_time = _env_str("EOD_TIME", "23:59")
    try:
        schedule.every().day.at(eod_time).do(_send_daily_report)
        logging.info("daily report scheduled at %s", eod_time)
    except schedule.ScheduleValueError as exc:
        logging.warning("invalid EOD_TIME %s: %s", eod_time, exc)

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
            logging.info("health ping scheduled every %s minute(s)", interval)
        except schedule.ScheduleValueError as exc:
            logging.warning("health ping schedule error: %s", exc)
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

    # Periodic holdings refresh for guards/users
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

            if time.time() - last_hold >= holdings_refresh:
                snapshot = get_wallet_snapshot(_wallet_address) or {}
                update_snapshot(snapshot, time.time())
                try:
                    set_holdings(set(snapshot.keys()))
                except Exception as e:
                    logging.debug("set_holdings failed: %s", e)
                if not snapshot:
                    try:
                        send_telegram_messages([_diagnostics_empty_snapshot(_wallet_address or "")])
                    except Exception:
                        pass
                last_hold = time.time()

        except Exception as exc:
            logging.exception("loop error: %s", exc)
            now = time.monotonic()
            global _last_error_ts
            if now - _last_error_ts >= 120:
                try:
                    notify_error("runtime loop", exc)
                except Exception:
                    pass
                _last_error_ts = now

        time.sleep(max(0.5, _env_float("WALLET_POLL", 15.0) - (time.time() - t0)))
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        logging.exception("fatal: %s", exc)
        try:
            notify_error("fatal", exc)
        except Exception:
            pass
        sys.exit(1)
