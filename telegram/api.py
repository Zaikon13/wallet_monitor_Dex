from __future__ import annotations

"""Telegram API helpers."""

import logging
import os
from typing import Iterable

import requests

from telegram.formatters import chunk, escape_md_v2

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or ""

_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

log = logging.getLogger(__name__)


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
        response = requests.post(f"{_API_BASE}/sendMessage", data=data, timeout=30)
        return response.status_code == 200
    except Exception:
        log.debug("Telegram send failed", exc_info=True)
        return False


def _escape_parts(parts: Iterable[str], escape: bool) -> Iterable[tuple[str, bool]]:
    for raw_part in parts:
        if not escape:
            yield raw_part, False
            continue
        try:
            yield escape_md_v2(raw_part), True
        except Exception:
            log.debug("Markdown escape failed; falling back to plain text", exc_info=True)
            yield raw_part, False


def send_telegram(text: str, escape: bool = True) -> None:
    """High-level sender with safe chunking and MarkdownV2 escaping."""
    if not text:
        return

    parts = list(chunk(text, 3800))
    for escaped_part, used_markdown in _escape_parts(parts, escape):
        ok = _tg_send_raw(escaped_part, use_markdown=used_markdown)
        if not ok and used_markdown:
            _tg_send_raw(escaped_part, use_markdown=False)


# Compatibility alias for legacy imports used across the codebase
send_telegram_message = send_telegram
