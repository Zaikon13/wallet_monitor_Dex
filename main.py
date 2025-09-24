import os, sys, time, logging, signal, requests, threading
from dotenv import load_dotenv
import schedule
from reports.scheduler import run_pending
from reports.day_report import build_day_report_text
from telegram.api import send_telegram
try:
    from telegram.api import telegram_long_poll_loop  # new canonical name
except ImportError:  # pragma: no cover - fallback when legacy bundle lacks the helper
    telegram_long_poll_loop = None
from telegram.dispatcher import dispatch
from core.watch import make_from_env
from core.wallet_monitor import make_wallet_monitor
from core.providers.cronos import fetch_wallet_txs
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

def _setup_logging():
    level=os.getenv("LOG_LEVEL","INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

def _handle(sig, frm):
    global _shutdown; _shutdown=True

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
    except Exception as exc:
        logging.exception("daily report generation failed: %s", exc)
        send_telegram("⚠️ Failed to generate daily report.", dedupe=False)
        return
    send_telegram("📒 Daily Report\n" + report, dedupe=False)


def _send_health_ping() -> None:
    send_telegram("✅ alive", dedupe=False)


def _start_telegram_thread() -> None:
    if not callable(telegram_long_poll_loop):
        return
    tg_thread = threading.Thread(target=telegram_long_poll_loop, args=(dispatch,), daemon=True)
    tg_thread.start()


def main()->int:
    global _last_error_ts
    load_dotenv(); _setup_logging()
    send_telegram("✅ Cronos DeFi Sentinel started and is online.")
    eod_time = _env_str("EOD_TIME", "23:59")
    try:
        schedule.every().day.at(eod_time).do(_send_daily_report)
    except schedule.ScheduleValueError as exc:
        logging.warning("invalid EOD_TIME %s: %s", eod_time, exc)
    else:
        logging.info("daily report scheduled at %s", eod_time)
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
    watcher=make_from_env()
    wallet_mon=make_wallet_monitor(provider=fetch_wallet_txs)
    try: start_signals_server_if_enabled()
    except Exception as e: logging.warning("signals server error: %s", e)
    signal.signal(signal.SIGTERM, _handle); signal.signal(signal.SIGINT, _handle)
    holdings_refresh = int(os.getenv("HOLDINGS_REFRESH_SEC","60") or 60); last_hold=0.0
    wallet=os.getenv("WALLET_ADDRESS","")
    poll=int(os.getenv("WALLET_POLL","15") or 15)
    _start_telegram_thread()
    while not _shutdown:
        t0=time.time()
        try:
            watcher.poll_once()
            wallet_mon.poll_once()
            run_pending()
            if time.time()-last_hold >= holdings_refresh:
                snap=get_wallet_snapshot(wallet) or {}
                set_holdings(set(snap.keys()))
                last_hold=time.time()
        except Exception as e:
            logging.exception("loop error: %s", e)
            now = time.monotonic()
            if now - _last_error_ts >= 120:
                send_telegram("⚠️ runtime error (throttled)", dedupe=False)
                _last_error_ts = now
        time.sleep(max(0.5, poll - (time.time()-t0)))
    return 0

if __name__=="__main__":
    try: sys.exit(main())
    except Exception as e:
        logging.exception("fatal: %s", e)
        try: send_telegram(f"💥 Fatal error: {e}")
        except Exception: pass
        sys.exit(1)
