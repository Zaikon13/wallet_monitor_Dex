import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

import requests

from .formatters import escape_md

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


def _resolve_dedup_window(raw: str) -> float:
    if not raw:
        return 60.0
    try:
        value = float(raw)
    except ValueError:
        logging.debug("invalid TG_DEDUP_WINDOW_SEC value: %s", raw)
        return 60.0
    return max(0.0, value)


DEDUP_WINDOW_SEC = _resolve_dedup_window(os.getenv("TG_DEDUP_WINDOW_SEC", "").strip())
_last_message_text: Optional[str] = None
_last_message_ts: Optional[float] = None


def _post(payload: Dict[str, Any]) -> Tuple[bool, int, Any]:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        response = requests.post(url, json=payload, timeout=10)
    except Exception as exc:  # pragma: no cover - network failure logging only
        logging.debug("telegram send failed: %s", exc)
        return False, 0, str(exc)
    return response.ok, response.status_code, response.text


def send_telegram(
    text: str, parse_mode: Optional[str] = None, dedupe: bool = True
) -> Tuple[bool, int, Any]:
    """Send a Telegram message.

    Defaults to plain text. When MarkdownV2 is explicitly requested, escape the
    payload before sending it to Telegram.
    """
    global _last_message_text, _last_message_ts

    mode = parse_mode or None

    if mode == "MarkdownV2":
        text = escape_md(text)

    if dedupe and _last_message_text == text and _last_message_ts is not None:
        window = DEDUP_WINDOW_SEC
        if window > 0 and (time.monotonic() - _last_message_ts) < window:
            return True, 0, "deduped"

    if not TOKEN or not CHAT_ID:
        logging.info("[telegram] %s", text)
        return False, 0, {"ok": False, "error": "missing credentials"}

    payload: Dict[str, Any] = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    if mode:
        payload["parse_mode"] = mode

    ok, status_code, response = _post(payload)
    if ok:
        _last_message_text = text
        _last_message_ts = time.monotonic()
    return ok, status_code, response


def send_telegram_message(text: str) -> None:
    """Backward compatible helper used by legacy modules."""
    send_telegram(text)
