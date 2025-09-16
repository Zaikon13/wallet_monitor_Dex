# -*- coding: utf-8 -*-
import requests, time, math, logging

from os import getenv

BOT_TOKEN = getenv("TELEGRAM_BOT_TOKEN") or ""
CHAT_ID   = getenv("TELEGRAM_CHAT_ID") or ""
API_URL   = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
MAX_LEN   = 4096

log = logging.getLogger("telegram")

_MD_CHARS = r'_\*\[\]\(\)~`>#+-=|{}.!'

def escape_md(s: str) -> str:
    if not s: return s
    out=[]
    for ch in s:
        if ch in _MD_CHARS:
            out.append("\\"+ch)
        else:
            out.append(ch)
    return "".join(out)

def _post(text: str, parse_mode="MarkdownV2"):
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    backoff = 1.0
    for attempt in range(5):
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
            backoff = min(backoff*2, 8.0)

def send_telegram(text: str):
    """
    Escapes MarkdownV2 and chunks messages > 4096 chars.
    """
    if not BOT_TOKEN or not CHAT_ID or not text:
        return
    esc = escape_md(text)
    # chunk by MAX_LEN
    if len(esc) <= MAX_LEN:
        _post(esc)
        return
    # split at line boundaries where possible
    start=0
    idx=0
    while start < len(esc):
        end = min(len(esc), start + MAX_LEN)
        # try break at last newline
        nl = esc.rfind("\n", start, end)
        if nl == -1 or nl <= start + 100:
            nl = end
        part = esc[start:nl]
        idx += 1
        header = f"_part {idx}_\n" if idx>1 else ""
        _post(header + part)
        start = nl

# exported for other modules
__all__ = ["send_telegram", "escape_md"]
