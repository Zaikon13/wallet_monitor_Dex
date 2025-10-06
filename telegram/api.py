# -*- coding: utf-8 -*-
"""Telegram API helpers."""
from __future__ import annotations

import os

import requests

from telegram.formatters import chunk, escape_md_v2

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _tg_send_raw(text: str, use_markdown: bool = True) -> bool:
    """Low-level sender. Returns True on HTTP 200."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        data = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": True,
        }
        if use_markdown:
            data["parse_mode"] = "MarkdownV2"
        r = requests.post(f"{_API_BASE}/sendMessage", data=data, timeout=30)
        return r.status_code == 200
    except Exception:
        return False


def send_telegram(text: str, escape: bool = True) -> None:
    """High-level sender with safe chunking and MarkdownV2 escaping."""
    if not text:
        return
    payload_text = escape_md_v2(text) if escape else text
    for part in chunk(payload_text, 3800):
        ok = _tg_send_raw(part, use_markdown=True)
        if not ok:
            _tg_send_raw(part, use_markdown=False)


# Compatibility alias for legacy imports used across the codebase
send_telegram_message = send_telegram
