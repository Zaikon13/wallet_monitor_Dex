"""Simple Telegram command dispatcher.
Returns plain text (Markdown-friendly) for recognized commands.
Main loop should call `dispatch(text)` and send the returned string.
"""
from __future__ import annotations
from typing import Optional

from telegram.commands import (
    handle_holdings,
    handle_show,
    handle_showdaily,
)

ALIASES = {
    "/holdings": handle_holdings,
    "/show_wallet_assets": handle_holdings,
    "/showwalletassets": handle_holdings,
    "/show": handle_show,
    "/status": handle_show,
    "/report": handle_showdaily,
    "/showdaily": handle_showdaily,
    "/dailysum": handle_showdaily,
}


def dispatch(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = text.strip().split()[0].lower()
    func = ALIASES.get(t)
    if not func:
        return None
    try:
        return func()
    except Exception as e:
        return f"⚠️ Command failed: {e}"
      
