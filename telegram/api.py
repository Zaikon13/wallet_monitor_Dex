import logging
import os
from typing import Any, Dict, Optional, Tuple

import requests

from .formatters import escape_md

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _post(payload: Dict[str, Any]) -> Tuple[bool, int, Any]:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        response = requests.post(url, json=payload, timeout=10)
    except Exception as exc:  # pragma: no cover - network failure logging only
        logging.debug("telegram send failed: %s", exc)
        return False, 0, str(exc)
    return response.ok, response.status_code, response.text


def send_telegram(text: str, parse_mode: Optional[str] = None) -> Tuple[bool, int, Any]:
    """Send a Telegram message.

    Defaults to plain text. When MarkdownV2 is explicitly requested, escape the
    payload before sending it to Telegram.
    """
    if parse_mode == "MarkdownV2":
        text = escape_md(text)

    if not TOKEN or not CHAT_ID:
        logging.info("[telegram] %s", text)
        return False, 0, {"ok": False, "error": "missing credentials"}

    payload: Dict[str, Any] = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    return _post(payload)


def send_telegram_message(text: str) -> None:
    """Backward compatible helper used by legacy modules."""
    send_telegram(text)
