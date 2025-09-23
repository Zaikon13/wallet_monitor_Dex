# main.py
import os, sys, time, logging, signal, requests
from dotenv import load_dotenv

# ---- Config / Validation ----
from core.config import apply_env_aliases, validate_env

# ---- Scheduler ----
from reports.scheduler import start_eod_scheduler, run_pending

# ---- Telegram ----
from telegram.api import send_telegram_message
import telegram.dispatcher as tdisp  # we'll call functions defensively

# ---- Watcher (safe fallback if not implemented) ----
try:
    from core.watch import make_from_env as _make_from_env
except Exception:
    class _NoopWatcher:
        def poll_once(self): return 0
    def _make_from_env(): return _NoopWatcher()

# ---- Holdings (safe fallback if not implemented) ----
try:
    from core.holdings import get_wallet_snapshot as _get_wallet_snapshot
except Exception:
    def _get_wallet_snapshot(_wallet: str) -> dict: return {}

# ---- Wallet monitor / Providers / Signals ----
from core.wallet_monitor import make_wallet_monitor
from core.providers.cronos import fetch_wallet_txs
from core.signals.server import start_signals_server_if_enabled
from core.guards import set_holdings

_shutdown = False
_updates_offset = None


def _setup_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _handle(_sig, _frm):
    global _shutdown
    _shutdown = True


def _dispatch_safe(text: str, chat_id=None):
    """
    Call telegram.dispatcher.dispatch(text, chat_id) if it exists;
    otherwise fall back to _dispatch(text). Always return a reply or None.
    """
    fn = getattr(tdisp, "dispatch", None)
    if callable(fn):
        return fn(text, chat_id)
    fn2 = getattr(tdisp, "_dispatch", None)
    if callable(fn2):
        fn2(text)
    return None


def _poll_telegram_once():
    global _updates_offset
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            return
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        params = {"timeout": 10}
        if _updates_offset is not None:
            params["offset"] = _updates_offset
        r = requests.get(url, params=params, timeout=15)
        if not r.headers.get("content-type", "").startswith("application/json"):
            return
        resp = r.json()
        if not resp.get("ok", True):
            return
        for upd in resp.get("result", []):
            _updates_offset = max(_updates_offset or 0, upd.get("update_id", 0) + 1)
            msg = upd.get("message") or upd.get("edited_message") or {}
            if not msg:
                continue
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text")
            if not text:
                continue
            reply = _dispatch_safe(text, chat_id)
            if reply:
                send_telegram_message(reply)
    except Exception as e:
        logging.debug("telegram poll failed: %s", e)


def main() -> int:
    load_dotenv()
    apply_env_aliases()
    validate_env(strict=False)
    _setup_logging()

    send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")

    try:
        start_eod_scheduler()
    except Exception as e:
        logging.warning("EOD scheduler error: %s", e)

    watcher = _make_from_env()
    wallet_mon = make_wallet_monitor(provider=fetch_wallet_txs)

    try:
        start_signals_server_if_enabled()
    except Exception as e:
        logging.warning("signals server error: %s", e)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    holdings_refresh = int(os.getenv("HOLDINGS_REFRESH_SEC", "60") or 60)
    last_hold = 0.0
    wallet = os.getenv("WALLET_ADDRESS", "")
    poll = int(os.getenv("WALLET_POLL", "15") or 15)

    while not _shutdown:
        t0 = time.time()
        try:
            watcher.poll_once()
            wallet_mon.poll_once()
            run_pending()

            if time.time() - last_hold >= holdings_refresh:
                snap = _get_wallet_snapshot(wallet) or {}
                set_holdings(set(snap.keys()))
                last_hold = time.time()

            _poll_telegram_once()
        except Exception as e:
            logging.exception("loop error: %s", e)
        time.sleep(max(0.5, poll - (time.time() - t0)))

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        logging.exception("fatal: %s", e)
        try:
            send_telegram_message(f"💥 Fatal error: {e}")
        except Exception:
            pass
        sys.exit(1)
