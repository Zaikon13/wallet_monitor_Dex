from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

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
_MAX_MESSAGE_LEN = 4096


def _post(payload: Dict[str, Any]) -> Tuple[bool, int, Any]:
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        response = requests.post(url, json=payload, timeout=10)
    except Exception as exc:  # pragma: no cover - network failure logging only
        logging.debug("telegram send failed: %s", exc)
        return False, 0, str(exc)
    return response.ok, response.status_code, response.text


def _chunk_text(text: str, limit: int = _MAX_MESSAGE_LEN) -> List[str]:
    if text is None:
        return []
    text = str(text)
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1 or split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    return chunks


def send_telegram(
    text: str,
    parse_mode: Optional[str] = None,
    dedupe: bool = True,
) -> Tuple[bool, int, Any]:
    """Send a Telegram message."""
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


def send_telegram_messages(messages: Iterable[str]) -> List[Tuple[bool, int, Any]]:
    results: List[Tuple[bool, int, Any]] = []
    for message in messages or []:
        for chunk in _chunk_text(message):
            results.append(send_telegram(chunk))
    return results


def send_telegram_message(text: str) -> None:
    """Backward compatible helper used by legacy modules."""
    send_telegram(text)


def telegram_long_poll_loop(dispatcher) -> None:
    if not TOKEN:
        logging.info("telegram token missing; skipping long poll loop")
        return

    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params: Dict[str, Any] = {"timeout": 20}
    offset: Optional[int] = None

    while True:
        try:
            if offset is not None:
                params["offset"] = offset
            response = requests.get(url, params=params, timeout=25)
            data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            updates = data.get("result", []) if data.get("ok", True) else []
        except Exception as exc:  # pragma: no cover - network failures
            logging.debug("telegram poll failed: %s", exc)
            time.sleep(2)
            continue

        for update in updates:
            try:
                offset = max(offset or 0, int(update.get("update_id", 0)) + 1)
            except Exception:
                continue
            message = update.get("message") or update.get("edited_message") or {}
            text = message.get("text")
            chat = message.get("chat") or {}
            chat_id = chat.get("id")
            if not text:
                continue
            replies = dispatcher(text, chat_id)
            if not replies:
                continue
            send_telegram_messages(replies)

        time.sleep(0.5)
