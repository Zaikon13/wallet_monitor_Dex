import os
import time
import math
import logging
import requests
from typing import Iterable

__all__ = ["send_telegram", "escape_md", "send_watchlist_alerts"]

log = logging.getLogger("telegram.api")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MAX_LEN = 4000  # leave room for MarkdownV2 quirks
API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def escape_md(text: str) -> str:
    """Minimal MarkdownV2 escaping."""
    if not text:
        return text
    special = "_[]()~`>#+-=|{}.!"
    out = []
    for ch in text:
        if ch in special:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _chunks(s: str, size: int) -> Iterable[str]:
    for i in range(0, len(s), size):
        yield s[i : i + size]


def _post(method: str, payload: dict, *, retries: int = 2):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"{API_URL}/{method}"
    backoff = 0.7
    for i in range(retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 429:
                # respect retry-after if provided
                try:
                    wait = int(r.json().get("parameters", {}).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                raise RuntimeError(f"5xx {r.status_code}")
            return
        except Exception as e:
            if i == retries:
                log.warning("Telegram send failed: %s", e)
                return
            time.sleep(backoff * (i + 1))


def send_telegram(text: str):
    if not text:
        return
    for part in _chunks(text, MAX_LEN):
        _post(
            "sendMessage",
            {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": part,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
        )
        time.sleep(0.05)


def send_watchlist_alerts(alerts: list[dict]):
    """
    Send formatted alerts for watchlist scanner.
    alerts: list of dict with keys {pair, token0, token1, price, change, vol24h, liq_usd, url}
    """
    if not alerts:
        return
    lines = ["*🔍 Watchlist Alerts*"]
    for a in alerts:
        pair = a.get("pair") or f"{a.get('token0','?')}/{a.get('token1','?')}"
        price = a.get("price")
        ch = a.get("change")
        vol = a.get("vol24h")
        liq = a.get("liq_usd")
        url = a.get("url", "")
        base = f"• {pair}: "
        if price is not None:
            base += f"${price:.6f}"
        if ch is not None:
            base += f" | Δ24h {ch:+.2f}%"
        if vol is not None:
            base += f" | Vol24h ${vol:,.0f}"
        if liq is not None:
            base += f" | Liq ${liq:,.0f}"
        if url:
            base += f"\n{url}"
        lines.append(base)
    send_telegram("\n".join(lines))
