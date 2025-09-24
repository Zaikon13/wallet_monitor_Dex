#!/usr/bin/env python3
# Cronos DeFi Sentinel — minimal, modular main.py
from __future__ import annotations

import os
import sys
import time
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional

# ---- Core config / TZ --------------------------------------------------------
try:
    from core.config import apply_env_aliases
except Exception:
    def apply_env_aliases() -> None:
        pass

try:
    from core.tz import ATHENS as TZ_ATHENS, now_gr, ymd
except Exception:
    from zoneinfo import ZoneInfo
    TZ_ATHENS = ZoneInfo("Europe/Athens")
    def now_gr() -> datetime: return datetime.now(TZ_ATHENS)  # type: ignore
    def ymd() -> str: return now_gr().strftime("%Y-%m-%d")    # type: ignore

# ---- Reports -----------------------------------------------------------------
try:
    from reports.day_report import build_intraday_report_text, build_day_report_text
except Exception:
    def build_intraday_report_text(*_, **__): return "🟡 Intraday Update\n(n/a)"
    def build_day_report_text(*_, **__):      return "📒 Daily Report\n(n/a)"

try:
    from reports.weekly import build_weekly_report_text  # optional
except Exception:
    def build_weekly_report_text(*_, **__):  # type: ignore
        return "🧾 Weekly PnL (n/a)"

# ---- Telegram glue -----------------------------------------------------------
try:
    from telegram.api import send_telegram_message
except Exception:
    def send_telegram_message(text: str) -> None:
        logging.info("[telegram disabled] %s", text)

try:
    # dispatcher.dispatch() δρομολογεί τις εντολές στα handlers (commands.py)
    from telegram.dispatcher import dispatch as telegram_dispatch
except Exception:
    def telegram_dispatch(text: str, chat_id: Optional[int] = None) -> Optional[str]:  # type: ignore
        return "Unknown command."

# ---- HTTP (requests) ---------------------------------------------------------
try:
    import requests
except Exception:
    requests = None  # type: ignore

# ==============================================================================
# Logging & ENV
# ==============================================================================
def setup_logging() -> None:
    lvl = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

def require_env(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env: {key}")
    return val

# ==============================================================================
# Telegram Long Poll (minimal & resilient)
# ==============================================================================
class TelegramPoller(threading.Thread):
    def __init__(self, token: str, chat_id: str, cooldown_sec: int = 1):
        super().__init__(daemon=True)
        self.token = token
        self.chat_id = chat_id
        self.cooldown = max(1, cooldown_sec)
        self._stop = threading.Event()
        self._offset_file = os.getenv("TG_OFFSET_FILE", ".telegram_offset.json")

    def _api(self, method: str, **params):
        if requests is None:
            return None
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
                data = json.load(f)
                return int(data.get("offset") or 0)
        except Exception:
            return 0

    def _save_offset(self, off: int) -> None:
        try:
            with open(self._offset_file, "w", encoding="utf-8") as f:
                json.dump({"offset": off}, f)
        except Exception:
            pass

    def run(self) -> None:
        logging.info("Telegram poller started.")
        offset = self._load_offset()
        backoff = 1
        while not self._stop.is_set():
            if requests is None:
                time.sleep(5)
                continue
            try:
                resp = self._api("getUpdates", timeout=25, offset=offset + 1, allowed_updates=["message"])
                if not resp or not resp.get("ok", False):
                    time.sleep(min(30, backoff))
                    backoff = min(30, backoff * 2)
                    continue
                backoff = 1  # reset
                updates = resp.get("result") or []
                for u in updates:
                    try:
                        offset = int(u.get("update_id") or offset)
                        self._save_offset(offset)
                        msg = (u.get("message") or {})
                        text = (msg.get("text") or "").strip()
                        chat = msg.get("chat") or {}
                        cid = chat.get("id")
                        if not text:
                            continue
                        # route command to dispatcher
                        reply = telegram_dispatch(text, chat_id=cid)
                        if isinstance(reply, str) and reply.strip():
                            send_telegram_message(reply)
                    except Exception:
                        logging.exception("Error handling update")
                time.sleep(self.cooldown)
            except Exception:
                logging.exception("Telegram poller loop error")
                time.sleep(min(30, backoff))
                backoff = min(30, backoff * 2)

    def stop(self) -> None:
        self._stop.set()

# ==============================================================================
# EOD Daily Scheduler
# ==============================================================================
def _parse_eod_time() -> tuple[int, int]:
    """
    Returns (hour, minute) from env:
      - EOD_TIME = 'HH:MM' (preferred)
      - or EOD_HOUR / EOD_MINUTE
    Defaults to 23:59.
    """
    t = (os.getenv("EOD_TIME") or "").strip()
    if t and ":" in t:
        try:
            hh, mm = t.split(":", 1)
            return max(0, min(23, int(hh))), max(0, min(59, int(mm)))
        except Exception:
            pass
    try:
        hh = int(os.getenv("EOD_HOUR", "23"))
        mm = int(os.getenv("EOD_MINUTE", "59"))
        return max(0, min(23, hh)), max(0, min(59, mm))
    except Exception:
        return 23, 59

def _seconds_until(hour: int, minute: int, tz=TZ_ATHENS) -> float:
    now = now_gr()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    return (target - now).total_seconds()

def _send_daily_report(wallet: Optional[str] = None) -> None:
    try:
        txt = build_day_report_text(wallet=wallet)
        send_telegram_message(f"📒 Daily Report\n{txt}")
    except Exception:
        logging.exception("Failed to build or send daily report")
        try:
            send_telegram_message("⚠️ Failed to generate daily report.")
        except Exception:
            pass

class EODThread(threading.Thread):
    def __init__(self, wallet: Optional[str] = None):
        super().__init__(daemon=True)
        self.wallet = wallet
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        hh, mm = _parse_eod_time()
        logging.info("EOD scheduler armed for %02d:%02d Europe/Athens", hh, mm)
        while not self._stop.is_set():
            try:
                wait_s = _seconds_until(hh, mm)
                if wait_s > 0:
                    # Sleep in small chunks to be interruptible
                    end = time.time() + wait_s
                    while time.time() < end and not self._stop.is_set():
                        time.sleep(min(30.0, end - time.time()))
                if self._stop.is_set():
                    break
                _send_daily_report()
            except Exception:
                logging.exception("EOD scheduler loop error")
                time.sleep(5)

# ==============================================================================
# Main
# ==============================================================================
def main() -> int:
    apply_env_aliases()
    setup_logging()

    # Required env for Telegram loops (send_telegram_message handles empty silently)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    try:
        send_telegram_message("✅ Cronos DeFi Sentinel started and is online.")
    except Exception:
        logging.debug("Startup telegram failed.", exc_info=True)

    # Start Telegram poller only if we have token+chat
    poller = None
    if token and chat_id:
        poller = TelegramPoller(token, chat_id, cooldown_sec=int(os.getenv("WALLET_POLL", "5") or 5))
        poller.start()
    else:
        logging.info("Telegram token/chat missing — running without long-poll.")

    # Arm EOD scheduler
    eod = EODThread(wallet=os.getenv("WALLET_ADDRESS", "").strip() or None)
    eod.start()

    # Keep the process alive (ctrl+c or TERM to exit)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        logging.info("Shutting down…")
    finally:
        try:
            if poller: poller.stop()
        except Exception:
            pass
        try:
            eod.stop()
        except Exception:
            pass
    return 0

if __name__ == "__main__":
    sys.exit(main())
