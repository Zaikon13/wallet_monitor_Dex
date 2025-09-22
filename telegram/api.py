import os
import time
import logging
import requests
from typing import Optional

BOT = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")
API = f"https://api.telegram.org/bot{BOT}"


def _escape_md(text: str) -> str:
    # minimal escaping for MarkdownV2 / code blocks
    return text.replace("(", "\\(").replace(")", "\\)")


def send_telegram_message(text: str, parse_mode: Optional[str] = None) -> bool:
    try:
        payload = {"chat_id": CHAT, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        r = requests.post(f"{API}/sendMessage", json=payload, timeout=10)
        if r.status_code != 200:
            logging.warning(f"Telegram send failed {r.status_code}: {r.text}")
        return r.ok
    except Exception as e:
        logging.exception(f"Telegram error: {e}")
        return False
