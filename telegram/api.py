# telegram/api.py
import os, time, logging, requests
from typing import Iterable

__all__ = ["send_telegram", "escape_md", "send_watchlist_alerts"]

log = logging.getLogger("telegram.api")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MAX_LEN = 4000


def escape_md(text: str) -> str:
    """Minimal MarkdownV2 escaping"""
    special = "_[]()~`>#+-=|{}.!"
    out = []
    for ch in text or "":
        out.append("\\" + ch if ch in special else ch)
    return "".join(out)


def _chunks(s: str, size: int) -> Iterable[str]:
    for i in range(0, len(s), size):
        yield s[i : i + size]


def _post(method: str, payload: dict, retries: int = 2):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"{API_URL}/{method}"
    for i in range(retries + 1):
        try:
            r = requests.post(url, json=payload, timeout=15)
            if r.status_code == 429:
                wait = int(r.json().get("parameters", {}).get("retry_after", 2))
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                raise RuntimeError(f"5xx {r.status_code}")
            return
        except Exception as e:
            if i == retries:
                log.warning("Telegram send failed: %s", e)
                return
            time.sleep(1.5 * (i + 1))


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
    """Alerts από watchlist scanner"""
    if not alerts:
        return
    lines = ["*🔍 Watchlist Alerts*"]
    for a in alerts:
        pair = a.get("pair") or f"{a.get('token0','?')}/{a.get('token1','?')}"
        base = f"• {pair}"
        if a.get("price") is not None:
            base += f" @ ${a['price']:.6f}"
        if a.get("change") is not None:
            base += f" | Δ24h {a['change']:+.2f}%"
        if a.get("vol24h") is not None:
            base += f" | Vol24h ${a['vol24h']:,.0f}"
        if a.get("liq_usd") is not None:
            base += f" | Liq ${a['liq_usd']:,.0f}"
        if a.get("url"):
            base += f"\n{a['url']}"
        lines.append(base)
    send_telegram("\n".join(lines))
