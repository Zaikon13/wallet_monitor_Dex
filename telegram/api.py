# telegram/api.py
import os
import logging
from utils.http import safe_get

log = logging.getLogger("telegram")

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

def send_telegram(message: str) -> bool:
    """Αποστολή μηνύματος στο Telegram bot"""
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            log.warning("Telegram not configured.")
            return False
        url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        r = safe_get(url, params=payload, timeout=12, retries=2)
        if not r or r.status_code != 200:
            if r:
                log.warning("Telegram status %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.exception("send_telegram exception: %s", e)
        return False
