# telegram/api.py
import os, time, random, logging, requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
TG_MAX = 4096

log = logging.getLogger("telegram")

# χαρακτήρες που θέλουν escape στο MarkdownV2
MDV2_ESC = r'_*[]()~`>#+-=|{}.!'

def escape_md(text: str) -> str:
    """Escape κειμένου για Telegram MarkdownV2"""
    return ''.join('\\' + c if c in MDV2_ESC else c for c in (text or ""))

def _post(payload: dict) -> bool:
    """Αποστολή μηνύματος στο Telegram με retries + backoff"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    backoff = 2
    for attempt in range(6):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                try:
                    ra = r.json().get("parameters", {}).get("retry_after", 1)
                except Exception:
                    ra = 1
                log.warning("Rate-limited by Telegram, retrying after %ss", ra)
                time.sleep(ra + random.uniform(0, 0.5))
                continue
            # άλλα HTTP errors → μικρό backoff
            time.sleep(min(30, backoff + random.uniform(0, 1)))
            backoff = min(30, backoff * 2)
        except Exception as e:
            log.debug("telegram post error: %s", e)
            time.sleep(min(30, backoff + random.uniform(0, 1)))
            backoff = min(30, backoff * 2)
    return False

def send_telegram(text: str, parse_mode: str = "MarkdownV2") -> bool:
    """Στέλνει μήνυμα στο Telegram (κόβει >4096 chars σε chunks)"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    chunks = []
    s = text or ""
    while s:
        chunks.append(s[:TG_MAX])
        s = s[TG_MAX:]

    ok = True
    for i, ch in enumerate(chunks, 1):
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": escape_md(ch) if parse_mode == "MarkdownV2" else ch,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if len(chunks) > 1:
            payload["text"] = f"{payload['text']}\n\n_{i}/{len(chunks)}_"
        ok = _post(payload) and ok
    return ok
