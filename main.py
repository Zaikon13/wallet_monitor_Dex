import os, sys, time, logging, signal, requests, threading
from dotenv import load_dotenv
import logging
import os
import signal
import sys
import threading
import time
from typing import Optional

import schedule
from reports.scheduler import run_pending
from reports.day_report import build_day_report_text
from telegram.api import send_telegram
try:
    from telegram.api import telegram_long_poll_loop  # new canonical name
except ImportError:  # pragma: no cover - fallback when legacy bundle lacks the helper
    telegram_long_poll_loop = None
from telegram.dispatcher import dispatch
from dotenv import load_dotenv

from core.guards import set_holdings
from core.holdings import get_wallet_snapshot
from core.providers.cronos import fetch_wallet_txs
from core.runtime_state import note_tick, update_snapshot
from core.watch import make_from_env
from core.wallet_monitor import make_wallet_monitor
from core.providers.cronos import fetch_wallet_txs
from reports.day_report import build_day_report_text
from reports.weekly import build_weekly_report_text
from reports.scheduler import run_pending
from telegram.api import send_telegram, send_telegram_messages, telegram_long_poll_loop
from telegram.dispatcher import dispatch

try:
    from core.signals.server import start_signals_server_if_enabled
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    def start_signals_server_if_enabled():
        logging.warning("signals server disabled: Flask missing")
from core.holdings import get_wallet_snapshot
from core.guards import set_holdings

_shutdown=False
_updates_offset=None
_last_error_ts=float("-inf")

_shutdown = False
_last_error_ts = float("-inf")
_last_intraday_signature: Optional[tuple] = None
_wallet_address: Optional[str] = None


def _env_str(name: str, default: str = "") -> str:
@@ -43,81 +52,143 @@ def _env_float(name: str, default: float) -> float:
        logging.debug("invalid float env %s=%r", name, raw)
        return default

def _setup_logging():
    level=os.getenv("LOG_LEVEL","INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

def _handle(sig, frm):
    global _shutdown; _shutdown=True
def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _handle_shutdown(sig, frm):
    global _shutdown
    _shutdown = True


def _snapshot_signature(snapshot: dict) -> tuple:
    items = []
    for symbol, payload in sorted(snapshot.items()):
        qty = payload.get("qty") or payload.get("amount") or "0"
        usd = payload.get("usd") or payload.get("value_usd") or "0"
        items.append((symbol, str(qty), str(usd)))
    return tuple(items)

def _legacy_telegram_long_poll_loop(handler):
    global _updates_offset
    token = _env_str("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": 10}
    while not _shutdown:
        try:
            if _updates_offset is not None:
                params["offset"] = _updates_offset
            r = requests.get(url, params=params, timeout=15)
            resp = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"ok": False}
            if not resp.get("ok", True):
                continue
            for upd in resp.get("result", []):
                _updates_offset = max(_updates_offset or 0, upd.get("update_id", 0) + 1)
                msg = upd.get("message") or upd.get("edited_message") or {}
                if not msg:
                    continue
                chat_id = (msg.get("chat") or {}).get("id")
                text = msg.get("text")
                if not text:
                    continue
                reply = handler(text, chat_id)
                if reply:
                    send_telegram(reply)
        except Exception as e:
            logging.debug("telegram poll failed: %s", e)
        time.sleep(1)


if telegram_long_poll_loop is None:
    telegram_long_poll_loop = _legacy_telegram_long_poll_loop

def _send_daily_report() -> None:
    try:
        report = build_day_report_text(False)
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
    send_telegram("📒 Daily Report\n" + report, dedupe=False)
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

    if signature and signature == _last_intraday_signature:
        send_telegram("⌛ cooldown")
        return

    _last_intraday_signature = signature
    total_usd = 0.0
    top_symbol = None
    top_value = 0.0
    for symbol, payload in snapshot.items():
        try:
            usd = float(payload.get("usd") or payload.get("value_usd") or 0.0)
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


def _start_telegram_thread() -> None:
    if not callable(telegram_long_poll_loop):
        return
    tg_thread = threading.Thread(target=telegram_long_poll_loop, args=(dispatch,), daemon=True)
    tg_thread.start()


def main()->int:
    global _last_error_ts
    load_dotenv(); _setup_logging()
def main() -> int:
    global _wallet_address
    load_dotenv()
    _setup_logging()

    send_telegram("✅ Cronos DeFi Sentinel started and is online.")

    _wallet_address = _env_str("WALLET_ADDRESS", "")

    eod_time = _env_str("EOD_TIME", "23:59")
    try:
        schedule.every().day.at(eod_time).do(_send_daily_report)
    except schedule.ScheduleValueError as exc:
        logging.warning("invalid EOD_TIME %s: %s", eod_time, exc)
    else:
        logging.info("daily report scheduled at %s", eod_time)

    weekly_dow = _env_str("WEEKLY_DOW", "SUN")
    weekly_time = _env_str("WEEKLY_TIME", "18:00")
    _schedule_weekly_job(weekly_dow, weekly_time)

    health_min = _env_float("HEALTH_MIN", 30.0)
    if health_min > 0:
        interval = max(1, int(health_min))
@@ -129,38 +200,63 @@ def main()->int:
            logging.info("health ping scheduled every %s minute(s)", interval)
    else:
        logging.info("health ping disabled")
    watcher=make_from_env()
    wallet_mon=make_wallet_monitor(provider=fetch_wallet_txs)
    try: start_signals_server_if_enabled()
    except Exception as e: logging.warning("signals server error: %s", e)
    signal.signal(signal.SIGTERM, _handle); signal.signal(signal.SIGINT, _handle)
    holdings_refresh = int(os.getenv("HOLDINGS_REFRESH_SEC","60") or 60); last_hold=0.0
    wallet=os.getenv("WALLET_ADDRESS","")
    poll=int(os.getenv("WALLET_POLL","15") or 15)

    intraday_hours = _env_float("INTRADAY_HOURS", 1.0)
    if intraday_hours > 0:
        try:
            schedule.every(max(1, int(intraday_hours))).hours.do(_send_intraday_update)
        except schedule.ScheduleValueError as exc:
            logging.warning("intraday schedule error: %s", exc)
    else:
        logging.info("intraday updates disabled")

    watcher = make_from_env()
    wallet_mon = make_wallet_monitor(provider=fetch_wallet_txs)

    try:
        start_signals_server_if_enabled()
    except Exception as exc:
        logging.warning("signals server error: %s", exc)

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    holdings_refresh = int(os.getenv("HOLDINGS_REFRESH_SEC", "60") or 60)
    last_hold = 0.0

    _start_telegram_thread()

    while not _shutdown:
        t0=time.time()
        t0 = time.time()
        try:
            watcher.poll_once()
            wallet_mon.poll_once()
            note_tick()
            run_pending()
            if time.time()-last_hold >= holdings_refresh:
                snap=get_wallet_snapshot(wallet) or {}
                set_holdings(set(snap.keys()))
                last_hold=time.time()
        except Exception as e:
            logging.exception("loop error: %s", e)
            if time.time() - last_hold >= holdings_refresh:
                snapshot = get_wallet_snapshot(_wallet_address) or {}
                update_snapshot(snapshot, time.time())
                set_holdings(set(snapshot.keys()))
                last_hold = time.time()
        except Exception as exc:
            logging.exception("loop error: %s", exc)
            now = time.monotonic()
            global _last_error_ts
            if now - _last_error_ts >= 120:
                send_telegram("⚠️ runtime error (throttled)", dedupe=False)
                _last_error_ts = now
        time.sleep(max(0.5, poll - (time.time()-t0)))
        sleep_for = max(0.5, _env_float("WALLET_POLL", 15.0) - (time.time() - t0))
        time.sleep(sleep_for)
    return 0

if __name__=="__main__":
    try: sys.exit(main())
    except Exception as e:
        logging.exception("fatal: %s", e)
        try: send_telegram(f"💥 Fatal error: {e}")
        except Exception: pass

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
