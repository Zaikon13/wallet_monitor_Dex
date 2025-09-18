#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram API helper — robust sending
- Uses HTML parse mode by default to avoid MarkdownV2 pitfalls (e.g., '.' needing escaping)
- Escapes content safely, supports chunking (Telegram limit ~4096 chars)
- Handles 429 rate limits (retry_after) and 400 parse errors with fallbacks

ENV:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
from __future__ import annotations

import os
import time
import json
import html
import logging
from typing import Iterable, Optional
import re

import requests

log = logging.getLogger("telegram.api")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None

# 4096 is Telegram's hard cap for text; keep headroom for HTML entities
_MAX_LEN = 3500


def _split_chunks(text: str, max_len: int = _MAX_LEN) -> Iterable[str]:
    """Split text into reasonably sized chunks, preferring newline boundaries."""
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    out = []
    buf = text
    while len(buf) > max_len:
        cut = buf.rfind("\n", 0, max_len)
        if cut == -1:
            cut = max_len
        out.append(buf[:cut])
        buf = buf[cut:]
        # trim leading newline to avoid empty chunks
        if buf.startswith("\n"):
            buf = buf[1:]
    if buf:
        out.append(buf)
    return out


def _post_json(method: str, payload: dict, timeout: int = 15) -> requests.Response:
    if not _API_BASE:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")
    url = f"{_API_BASE}/{method}"
    return requests.post(url, json=payload, timeout=timeout)


def _send_raw(text: str, *, parse_mode: Optional[str] = "HTML", disable_preview: bool = True) -> dict:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": disable_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    # Basic retry loop with 429 handling
    attempts = 0
    backoff = 0.6
    while True:
        attempts += 1
        r = _post_json("sendMessage", payload)
        if r.status_code == 200:
            return r.json()
        try:
            data = r.json()
        except Exception:
            data = {"description": r.text}

        desc = data.get("description") or r.text
        # Too Many Requests — respect retry_after
        if r.status_code == 429:
            retry_after = data.get("parameters", {}).get("retry_after", 1)
            wait_s = max(float(retry_after), backoff)
            log.warning("Telegram 429, sleeping %.2fs", wait_s)
            time.sleep(wait_s)
            backoff = min(backoff * 2, 8.0)
            continue

        # Parse error with entities — fallback strategy
        if r.status_code == 400 and "can't parse entities" in (desc or "").lower():
            # If we used HTML, drop parse_mode. If we used Markdown, switch to HTML.
            if parse_mode == "HTML":
                log.warning("Telegram send failed 400 (entities). Falling back to plain text.")
                return _send_raw(text, parse_mode=None, disable_preview=disable_preview)
            elif parse_mode == "MarkdownV2":
                log.warning("Telegram send failed 400 (entities). Falling back to HTML.")
                safe = html.escape(text)
                return _send_raw(safe, parse_mode="HTML", disable_preview=disable_preview)

        log.warning("Telegram send failed %s: %s", r.status_code, desc)
        if attempts >= 3:
            return data
        time.sleep(backoff)
        backoff = min(backoff * 2, 8.0)


def send_telegram(text: str, *, parse_mode: Optional[str] = "HTML", disable_preview: bool = True) -> None:
    """Public API: safely send a message to Telegram with chunking & fallbacks.

    - Defaults to HTML parse mode with **escaped** input to avoid Markdown issues.
    - If HTML fails to parse, automatically retries with plain text.
    - Splits long messages into multiple chunks.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM]", text)
        return

    if parse_mode == "HTML" and text:
        safe_text = html.escape(text)
    else:
        safe_text = text

    for idx, chunk in enumerate(_split_chunks(safe_text), 1):
        if idx > 1:
            time.sleep(0.15)
        _send_raw(chunk, parse_mode=parse_mode, disable_preview=disable_preview)


# ---------- Optional MarkdownV2 support for backward compatibility ----------
_MD_META = r"_\*\[\]\(\)~`>#+\-=|{}\.!"  # characters to escape in MarkdownV2
_MD_REGEX = re.compile(f"([{'\'.join(_MD_META)}])")

def escape_md(text: str) -> str:
    """Escape a string for Telegram MarkdownV2."""
    if not text:
        return text
    return _MD_REGEX.sub(r"\\\1", text)  # double-escape for safe passing


def send_markdown(text: str, *, disable_preview: bool = True) -> None:
    """Send as MarkdownV2 with proper escaping + chunking.
    Prefer `send_telegram` (HTML) for reliability. Use this only if you need Markdown features.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM-MD]", text)
        return
    safe_text = escape_md(text)
    for idx, chunk in enumerate(_split_chunks(safe_text), 1):
        if idx > 1:
            time.sleep(0.15)
        _send_raw(chunk, parse_mode="MarkdownV2", disable_preview=disable_preview)
