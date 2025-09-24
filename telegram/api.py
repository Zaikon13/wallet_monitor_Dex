from __future__ import annotations
import os, logging
from typing import Iterable
import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

_MD2_SPECIALS = r"_*[]()~`>#+-=|{}.!\\"

def _escape_markdown_v2(text: str) -> str:
    out = []
    for ch in text:
        if ch in _MD2_SPECIALS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)

def _chunks(s: str, size: int = 4096) -> Iterable[str]:
    for i in range(0, len(s), size):
        yield s[i:i+size]

def send_telegram_message(text: str, escape_md: bool = True) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        logging.error("Telegram env not set (BOT_TOKEN/CHAT_ID)")
        return
    msg = _escape_markdown_v2(text) if escape_md else text
    for part in _chunks(msg):
        try:
            resp = requests.post(
                API_URL,
                json={"chat_id": CHAT_ID, "text": part, "parse_mode": "MarkdownV2", "disable_web_page_preview": True},
                timeout=10,
            )
            if resp.status_code != 200:
                logging.warning("Telegram send failed %s: %s", resp.status_code, resp.text)
        except Exception as e:
            logging.exception("Telegram send error: %s", e)
