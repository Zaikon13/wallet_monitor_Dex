# -*- coding: utf-8 -*-
"""
Telegram API helpers.

Public API:
- send_telegram(text: str) -> None
- send_telegram_message = send_telegram  (compat alias for legacy imports)
"""
from __future__ import annotations

import math
import os
from typing import Iterable

import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _chunks(s: str, size: int = 3800) -> Iterable[str]:
    """Yield Telegram-safe chunks (4096 hard limit, keep headroom for Markdown)."""
    if not s:
        return []
    total = max(1, math.ceil(len(s) / size))
    for i in range(total):
        yield s[i * size : (i + 1) * size]


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
            data["parse_mode"] = "Markdown"
        r = requests.post(f"{_API_BASE}/sendMessage", data=data, timeout=30)
        return r.status_code == 200
    except Exception:
        return False


def send_telegram(text: str) -> None:
    """
    High-level sender with safe chunking and Markdown fallback.
    Returns None (fire-and-forget).
    """
    if not text:
        return
    for part in _chunks(text, 3800):
        ok = _tg_send_raw(part, use_markdown=True)
        if not ok:
            # Fallback: send without parse_mode (avoid Markdown parsing errors)
            _tg_send_raw(part, use_markdown=False)


# Compatibility alias for legacy imports used across the codebase
send_telegram_message = send_telegram
