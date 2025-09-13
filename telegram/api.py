# telegram/api.py
import re, time, random, logging, requests, os

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
TG_MAX = 4096

log = logging.getLogger("telegram")

MDV2_ESC = r'_*[]()~`>#+-=|{}.!'
def escape_md(text: str) -> str:
    return ''.join('\\'+c if c in MDV2_ESC else c for c in (text or ""))

def _post(payload):
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
                time.sleep(ra + random.uniform(0, 0.5))
                continue
            # άλλα λάθη: σύντομο backoff
            time.sleep(min(30, backoff + random.uniform(0, 1)))
            backoff = min(30, backoff * 2)
        except Exception as e:
            log.debug("telegram post error: %s", e)
            time.sleep(min(30, backoff + random.uniform(0, 1)))
            backoff = min(30, backoff * 2)
    return False

def send_telegram(text: str, parse_mode: str = "MarkdownV2"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    # chunk >4096
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
