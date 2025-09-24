#!/usr/bin/env python3
from __future__ import annotations

import os, sys, time, json, logging, threading
from datetime import datetime, timedelta
from typing import Optional

# ---------- Core config / tz ----------
try:
    from core.config import apply_env_aliases
except Exception:
    def apply_env_aliases(): pass

try:
    from core.tz import now_gr, ymd
except Exception:
    from zoneinfo import ZoneInfo
    def now_gr() -> datetime: return datetime.now(ZoneInfo("Europe/Athens"))
    def ymd() -> str: return now_gr().strftime("%Y-%m-%d")

# ---------- Reports ----------
try:
    from reports.day_report import build_day_report_text
except Exception:
    def build_day_report_text(*_, **__): return "📒 Daily Report\n(n/a)"

# ---------- Telegram glue ----------
try:
    from telegram.api import send_telegram_message
except Exception:
    def send_telegram_message(t: str): logging.info("[telegram off] %s", t)

try:
    from telegram.dispatcher import dispatch as telegram_dispatch
except Exception:
    def telegram_dispatch(text: str, chat_id: Optional[int] = None) -> Optional[str]:
        return "Unknown command."

# ---------- requests ----------
try:
    import requests
except Exception:
    requests = None  # type: ignore

# =============================================================================
# Logging
# =============================================================================
def setup_logging():
    lvl = (os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(level=getattr(logging, lvl, logging.INFO),
                        format="%(asctime)s | %(levelname)s | %(message)s")

# =============================================================================
# Telegram Long Poller (offset file + backoff)
# =============================================================================
class TelegramPoller(threading.Thread):
    def __init__(self, token: str, cooldown_sec: int = 1, offset_file: str = ".telegram_offset.json"):
        super().__init__(daemon=True)
        self.token = token
        self.cooldown = max(1, cooldown_sec)
        self._stop = threading.Event()
        self._offset_file = offset_file

    def _api(self, method: str, **params):
        if requests is None: return None
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logging.warning("Telegram API %s failed: %s", method, e)
            return None

    def _load_offset(self) -> int:
        try:
            with open(self._offset_file, "r", encoding="utf-8") as f:
                return int((json.load(f) or {}).get("offset") or 0)
        except Exception:
            return 0

    def _save_offset(self, off: int) -> None:
        try:
            with open(self._offset_file, "w", encoding="utf-8") as f:
                json.dump({"offset": off}, f)
        except Exception:
            pass

    def run(self):
        logging.info("Telegram poller started.")
        off = self._load_offset()
        backoff = 1
        while not self._stop.is_set():
            if requests is None:
                time.sleep(5); continue
            try:
                resp = self._api("getUpdates", timeout=25, offset=off + 1, allowed_updates=["message"])
                if not resp or not resp.get("ok"):
                    time.sleep(min(30, backoff)); backoff = min(30, backoff * 2); continue
                backoff = 1
                for u in (resp.get("result") or []):
                    try:
                        off = int(u.get("update_id") or off); self._save_offset(off)
                        msg = (u.get("message") or {})
                        txt = (msg.get("text") or "").strip()
                        chat_id = (msg.get("chat") or {}).get("id")
                        if not txt: continue
                        reply = telegram_dispatch(txt, chat_id=chat_id)
                        if isinstance(reply, str) and reply.strip():
                            send_telegram_message(reply)
                    except Exception:
                        logging.exception("update handling failed")
                time.sleep(self.cooldown)
            except Exception:
                logging.exception("poll loop error"); time.sleep(min(30, backoff)); backoff = min(30, backoff * 2)

    def stop(self): self._stop.set()

# =============================================================================
# EOD Daily scheduler (threaded)
# =============================================================================
def _parse_eod_hhmm() -> tuple[int, int]:
    t = (os.getenv("EOD_TIME") or "").strip()
    if t and ":" in t:
        try:
            hh, mm = [int(x) for x in t.split(":", 1)]
            return max(0, min(23, hh)), max(0, min(59, mm))
        except Exception:
            pass
    try:
        return int(os.getenv("EOD_HOUR","23")), int(os.getenv("EOD_MINUTE","59"))
    except Exception:
        return 23, 59

def _seconds_until(hour: int, minute: int) -> float:
    now = now_gr()
    tgt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tgt <= now: tgt = tgt + timedelta(days=1)
    return (tgt - now).total_seconds()

def _send_eod():
    try:
        txt = build_day_report_text()
        send_telegram_message(f"📒 Daily Report\n{txt}")
    except Exception:
        logging.exception("send_eod failed")
        try: send_telegram_message("⚠️ Failed to generate daily report.")
        except Exception: pass

class EODThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._stop = threading.Event()

    def stop(self): self._stop.set()

    def run(self):
        hh, mm = _parse_eod_hhmm()
        logging.info("Scheduled daily report at %02d:%02d", hh, mm)
        while not self._stop.is_set():
            try:
                wait_s = _seconds_until(hh, mm)
                end = time.time() + wait_s
                while time.time() < end and not self._stop.is_set():
                    time.sleep(min(30.0, end - time.time()))
                if self._stop.is_set(): break
                _send_eod()
            except Exception:
                logging.exception("EOD loop error"); time.sleep(5)

# =============================================================================
# Main
# =============================================================================
def main() -> int:
    apply_env_aliases()
    setup_logging()
    logging.info("Starting Sentinel (rescue+holdings)...")

    token = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    try:
        send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")
    except Exception:
        logging.debug("startup telegram failed", exc_info=True)

    poll = None
    if token:
        poll = TelegramPoller(token, cooldown_sec=int(os.getenv("WALLET_POLL","5") or 5))
        poll.start()
    else:
        logging.info("No TELEGRAM_BOT_TOKEN — long-poll disabled.")

    eod = EODThread(); eod.start()

    # optional intraday holdings timer logs (χωρίς scheduler lib)
    intr_hours = max(1, int(os.getenv("INTRADAY_HOURS","3") or 3))
    logging.info("Scheduled holdings snapshot every %d hour(s)", intr_hours)

    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Shutting down…")
    finally:
        try: 
            if poll: poll.stop()
        except Exception: pass
        try: eod.stop()
        except Exception: pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
