import time
from typing import Optional
try:
    from telegram.commands import handle_holdings, handle_show, handle_showdaily, handle_weekly
except Exception:
    def handle_holdings(): return "Holdings (n/a)"
    def handle_show(): return "Status (n/a)"
    def handle_showdaily(): return "Daily (n/a)"
    def handle_weekly(days: int = 7): return f"Weekly last {days}d (n/a)"

ALIASES = {
    "/holdings": handle_holdings,
    "/show": handle_show,
    "/status": handle_show,
    "/report": handle_showdaily,
    "/showdaily": handle_showdaily,
    "/weekly": handle_weekly,
}

_last = {}
COOL = 3

def _reportnow():
    try:
        from reports.day_report import build_day_report_text
        from telegram.api import send_telegram_message
        send_telegram_message(f"📒 Daily Report\n{build_day_report_text()}")
        return "Report requested."
    except Exception as e:
        return f"Failed: {e}"

ALIASES["/reportnow"] = _reportnow

def dispatch(text: str, chat_id: Optional[int] = None) -> Optional[str]:
    if not text: return None
    cmd = text.strip().split()[0].lower()
    fn = ALIASES.get(cmd)
    if not fn: return None
    now = time.time(); k = (cmd, int(chat_id) if chat_id else None)
    if now - _last.get(k, 0) < COOL: return "⌛ cooldown"
    _last[k] = now
    if fn is handle_weekly and len(text.split()) > 1:
        try: days = max(1, min(31, int(text.split()[1])))
        except: days = 7
        return fn(days=days)
    return fn()
