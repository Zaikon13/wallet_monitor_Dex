# telegram/dispatcher.py
from core.tz import now_local
from telegram.api import send_telegram_message
from telegram.formatters import escape

COMMAND_PREFIX = "/"
_handlers = {}

def register(cmd):
    def _wrap(fn):
        _handlers[cmd] = fn
        return fn
    return _wrap

@register("start")
@register("help")
def _help():
    send_telegram_message("Bot online. Use /status")

@register("status")
def _status():
    ts = now_local().strftime("%Y-%m-%d %H:%M:%S %Z")
    send_telegram_message(escape(f"🟢 Online: {ts}"))

def _dispatch(text: str):
    if not text or not text.startswith(COMMAND_PREFIX):
        return False
    cmd = text[1:].split()[0].lower()
    fn = _handlers.get(cmd)
    if fn:
        fn()
        return True
    return False

def dispatch(text: str, chat_id=None):
    _dispatch(text)
    return None
