import os, sys, time, logging, signal, requests, threading
import schedule
from dotenv import load_dotenv
try:
    from telegram.api import telegram_long_poll_loop, send_telegram
except ImportError:  # pragma: no cover - fallback when legacy bundle lacks the helper
    from telegram.api import send_telegram
    telegram_long_poll_loop = None
from reports.day_report import build_day_report_text
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

def _env_str(k, d=""): return os.getenv(k, d) or d
def _env_float(k, d): 
    try: return float(os.getenv(k, str(d)) or d)
    except: return d

def _setup_logging():
    level=os.getenv("LOG_LEVEL","INFO").upper()
    logging.basicConfig(level=getattr(logging, level, logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")

def _handle(sig, frm):
    global _shutdown; _shutdown=True

def _legacy_telegram_long_poll_loop(handler):
    global _updates_offset
    while not _shutdown:
        try:
            token=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
            if not token:
                time.sleep(5)
                continue
            url=f"https://api.telegram.org/bot{token}/getUpdates"
            params={"timeout": 10}
            if _updates_offset is not None:
                params["offset"]=_updates_offset
            r=requests.get(url, params=params, timeout=15)
            resp=r.json() if r.headers.get("content-type","").startswith("application/json") else {"ok": False}
            if not resp.get("ok", True):
                time.sleep(1)
                continue
            for upd in resp.get("result", []):
                _updates_offset = max(_updates_offset or 0, upd.get("update_id", 0) + 1)
                msg=upd.get("message") or upd.get("edited_message") or {}
                if not msg:
                    continue
                chat_id=(msg.get("chat") or {}).get("id")
                text=msg.get("text")
                if not text:
                    continue
                reply=handler(text, chat_id)
                if reply:
                    send_telegram(reply)
        except Exception as e:
            logging.debug("telegram poll failed: %s", e)
            time.sleep(1)
        else:
            time.sleep(0.1)


if telegram_long_poll_loop is None:
    telegram_long_poll_loop = _legacy_telegram_long_poll_loop

def _send_daily_report():
    try:
        text = build_day_report_text({})
        send_telegram("📒 Daily Report\n" + (text or "(empty)"))
    except Exception as e:
        send_telegram("⚠️ Failed to generate daily report.")
    return True

def main()->int:
    load_dotenv(); _setup_logging()
    send_telegram("✅ Cronos DeFi Sentinel started and is online.")
    EOD_TIME = _env_str("EOD_TIME", "23:59")
    schedule.every().day.at(EOD_TIME).do(_send_daily_report)
    logging.info("EOD daily report scheduled for %s", EOD_TIME)
    watcher=make_from_env()
    wallet_mon=make_wallet_monitor(provider=fetch_wallet_txs)
    try: start_signals_server_if_enabled()
    except Exception as e: logging.warning("signals server error: %s", e)
    signal.signal(signal.SIGTERM, _handle); signal.signal(signal.SIGINT, _handle)
    try:
        threading.Thread(target=telegram_long_poll_loop, args=(dispatch,), daemon=True).start()
    except Exception as e:
        logging.warning("telegram long-poll thread start failed: %s", e)
    holdings_refresh = _env_float("HOLDINGS_REFRESH_SEC", 60.0); last_hold=0.0
    wallet=os.getenv("WALLET_ADDRESS","")
    poll=_env_float("WALLET_POLL", 15.0)
    last_error_ts = 0.0
    while not _shutdown:
        t0=time.time()
        try:
            watcher.poll_once()
            wallet_mon.poll_once()
            schedule.run_pending()
            if time.time()-last_hold >= holdings_refresh:
                snap=get_wallet_snapshot(wallet) or {}
                set_holdings(set(snap.keys()))
                last_hold=time.time()
        except Exception as e:
            logging.exception("loop error: %s", e)
            now = time.time()
            if now - last_error_ts >= 120:
                try:
                    send_telegram("⚠️ runtime error (throttled)")
                except Exception:
                    pass
                last_error_ts = now
        time.sleep(max(0.5, poll - (time.time()-t0)))
    return 0

if __name__=="__main__":
    try: sys.exit(main())
    except Exception as e:
        logging.exception("fatal: %s", e)
        try: send_telegram(f"💥 Fatal error: {e}")
        except Exception: pass
        sys.exit(1)
