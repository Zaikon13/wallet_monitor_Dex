import os, time, logging, re
from typing import Optional

try:
    import requests
except Exception:
    requests = None  # type: ignore

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

_MD_SPECIALS = re.compile(r'([_*\[\]()~`>#+\-=|{}.!])')

def _escape_md(text: str) -> str:
    return _MD_SPECIALS.sub(r'\\\1', text or "")

def _post(text: str, chat_id: Optional[str] = None) -> bool:
    if requests is None:
        logging.info("[telegram noop] %s", text); return True
    chat = (chat_id or CHAT_ID).strip()
    if not TOKEN or not chat:
        logging.info("[telegram disabled] %s", text); return True
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat, "text": text, "parse_mode": "MarkdownV2", "disable_web_page_preview": True}
    backoff = 1
    for _ in range(4):
        try:
            r = requests.post(url, data=payload, timeout=20)
            if r.status_code == 200 and (r.json().get("ok") is True):
                return True
            logging.warning("telegram send non-200: %s %s", r.status_code, r.text[:180])
        except Exception as e:
            logging.warning("telegram send failed: %s", e)
        time.sleep(backoff); backoff = min(8, backoff * 2)
    return False

def send_telegram_message(text: str, chat_id: Optional[str] = None) -> None:
    """Safe send with MarkdownV2 escaping + chunking."""
    if not text: return
    msg = _escape_md(str(text))
    # Telegram limit ~4096 chars — σπάσε σε κομμάτια ~3500
    limit = 3500
    i = 0
    while i < len(msg):
        chunk = msg[i:i+limit]
        ok = _post(chunk, chat_id=chat_id)
        if not ok:
            logging.warning("telegram chunk failed")
            break
        i += limit
