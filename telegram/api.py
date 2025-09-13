# -*- coding: utf-8 -*-
"""
Telegram API helper
- MarkdownV2 escaping
- Chunking for >4096 chars
- Basic rate-limit handling (429 retry_after)
"""
import requests
import time
import logging
from os import getenv

BOT_TOKEN = getenv("TELEGRAM_BOT_TOKEN") or ""
CHAT_ID   = getenv("TELEGRAM_CHAT_ID") or ""
API_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
MAX_LEN   = 4096

log = logging.getLogger("telegram")

_MD_CHARS = r'_[]()~`>#+-=|{}.!*'

def escape_md(s: str) -> str:
    if not s:
        return s
    out = []
    for ch in s:
        if ch in _MD_CHARS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _post(text: str, parse_mode: str = "MarkdownV2"):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    backoff = 1.0
    for _ in range(5):
        try:
            r = requests.post(API_URL, json=payload, timeout=20)
            if r.status_code == 429:
                try:
                    retry_after = int(r.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    retry_after = 1
                time.sleep(retry_after + 0.2)
                continue
            if r.status_code >= 400:
                log.warning("Telegram send failed %s: %s", r.status_code, r.text[:200])
            return
        except Exception as e:
            log.debug("Telegram error: %s", e)
            time.sleep(backoff)
            backoff = min(backoff * 2, 8.0)


def send_telegram(text: str):
    """Escape MarkdownV2 + chunk >4096 into multiple messages."""
    if not BOT_TOKEN or not CHAT_ID or not text:
        return
    esc = escape_md(text)
    if len(esc) <= MAX_LEN:
        _post(esc)
        return

    start = 0
    idx = 0
    while start < len(esc):
        end = min(len(esc), start + MAX_LEN)
        nl = esc.rfind("\n", start, end)
        if nl == -1 or nl <= start + 100:
            nl = end
        part = esc[start:nl]
        idx += 1
        header = f"_part {idx}_\n" if idx > 1 else ""
        _post(header + part)
        start = nl


__all__ = ["send_telegram", "escape_md"]
