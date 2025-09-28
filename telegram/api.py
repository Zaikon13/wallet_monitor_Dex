# -*- coding: utf-8 -*-
import os
import math
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def _tg_send_raw(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        r = requests.post(
            f"{_API_BASE}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        return r.status_code == 200
    except Exception:
        return False

def _chunks(s: str, size: int = 3800):
    total = max(1, math.ceil(len(s) / size))
    for i in range(total):
        yield s[i*size:(i+1)*size]

def send_telegram(text: str) -> None:
    """
    High-level wrapper used by main.py.
    Splits very long messages to avoid Telegram 4096 char limit.
    """
    if not text:
        return
    for part in _chunks(text, 3800):
        ok = _tg_send_raw(part)
        if not ok:
            # last resort: send plain text without parse mode
            try:
                requests.post(
                    f"{_API_BASE}/sendMessage",
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": part,
                        "disable_web_page_preview": True,
                    },
                    timeout=30,
                )
            except Exception:
                pass
