# telegram/api.py
# Simple Telegram sender with safe MarkdownV2 escaping (or plain text)
from __future__ import annotations
import os
import json
from typing import Optional

try:
    from utils.http import post_json as _post_json  # type: ignore
except Exception:
    import requests

    def _post_json(url: str, payload: dict, timeout: int = 10):
        r = requests.post(url, json=payload, timeout=timeout)
        return r.status_code, r.text

MDV2_RESERVED = r'_\*\[\]\(\)~`>#+\-=|{}.!'

def escape_markdown_v2(text: str) -> str:
    if text is None:
        return ""
    # Πρώτα escape backslash
    text = text.replace("\\", "\\\\")
    # Μετά escape όλα τα ειδικά
    for ch in MDV2_RESERVED:
        text = text.replace(ch, f"\\{ch}")
    return text

def send_telegram(
    text: str,
    parse_mode: Optional[str] = None,
    disable_web_page_preview: bool = True,
    chat_id: Optional[str] = None,
    token: Optional[str] = None,
) -> tuple[bool, int, str]:
    """
    Στέλνει μήνυμα στο Telegram.
    - By default ΧΩΡΙΣ parse_mode (σκέτο text) για να μη σκάει.
    - Αν θες MarkdownV2, δώσε parse_mode='MarkdownV2' και θα γίνει escape αυτόματα.
    Διαβάζει TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID από το περιβάλλον αν δεν δοθούν.
    """
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False, 0, "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID"

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": str(chat_id),
        "disable_web_page_preview": disable_web_page_preview,
    }

    if parse_mode and parse_mode.upper() == "MARKDOWNV2":
        payload["parse_mode"] = "MarkdownV2"
        payload["text"] = escape_markdown_v2(text or "")
    elif parse_mode and parse_mode.upper() in ("MARKDOWN", "HTML"):
        # Δεν προτείνεται, αλλά το υποστηρίζουμε
        payload["parse_mode"] = parse_mode
        payload["text"] = text or ""
    else:
        # Plain text (ασφαλές)
        payload["text"] = text or ""

    try:
        status, resp = _post_json(url, payload, timeout=12)
        ok = (200 <= status < 300)
        return ok, status, resp
    except Exception as e:
        return False, 0, f"Exception: {e}"

def send_telegram_lines(lines: list[str], **kwargs) -> tuple[bool, int, str]:
    text = "\n".join(lines or [])
    return send_telegram(text, **kwargs)
