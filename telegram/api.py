# telegram/api.py
# -*- coding: utf-8 -*-

import os
import time
import threading
import requests

_send_lock = threading.Lock()
_last_payload = {"chat_id": None, "text": None}

def send_telegram(text: str, chat_id: str | None = None, parse_mode: str = "Markdown") -> bool:
    """
    Robust Telegram sender:
      - POST (όχι GET)
      - backoff & retry για 5xx και 429 (τιμά το retry_after)
      - απλή απο-διπλοποίηση για ίδια διαδοχικά μηνύματα
    """
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat  = chat_id or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat or not text:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }

    with _send_lock:
        global _last_payload
        # απλή dedupe: αν είναι *ακριβώς* ίδιο με το προηγούμενο, μην το ξαναστείλεις
        if _last_payload == {"chat_id": chat, "text": text}:
            return True

        backoff = 0.5
        for _ in range(5):
            try:
                r = requests.post(url, json=payload, timeout=15)
            except Exception:
                time.sleep(backoff)
                backoff = min(backoff * 2, 8)
                continue

            if r.status_code == 200:
                try:
                    j = r.json()
                except Exception:
                    j = {}
                if j.get("ok"):
                    _last_payload = {"chat_id": chat, "text": text}
                    return True

            if r.status_code == 429:
                # Floodwait: σεβάσου το retry_after
                try:
                    ra = float(r.json().get("parameters", {}).get("retry_after", 1))
                except Exception:
                    ra = 1.0
                time.sleep(max(backoff, ra))
            else:
                time.sleep(backoff)
                backoff = min(backoff * 2, 8)

    return False


__all__ = ["send_telegram"]
