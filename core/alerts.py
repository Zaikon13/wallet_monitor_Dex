# alerts.py
"""
Universal alerts adapter:
- Works with current project (telegram.api.send_telegram)
- Also works if a provider exists under core/providers (e.g. core.providers.telegram)
No changes needed in main.py.

Public API:
    notify_error(context: str, err: Exception | str) -> None
    notify_alert(text: str) -> None
    send_telegram_message(text: str) -> None   # legacy alias (if old code calls this)
"""

from __future__ import annotations
import os
import logging
import traceback
from typing import Callable, Optional

log = logging.getLogger("alerts")

# ------------------ Sender Discovery ------------------

def _discover_sender() -> Callable[[str], bool]:
    """
    Try multiple backends in priority order:
    1) telegram.api.send_telegram (our canonical)
    2) core.providers.telegram.(send_telegram|send_message|send_telegram_message)
    3) core.providers.alerts.(send_telegram|send_message|send_telegram_message)
    Returns a function(text) -> bool. If all fail, returns a no-op.
    """
    # 1) canonical
    try:
        from telegram.api import send_telegram as _send
        def _s1(text: str) -> bool:
            try:
                _send(text)
                return True
            except Exception as e:
                log.debug("telegram.api send failed: %s", e)
                return False
        return _s1
    except Exception as e:
        log.debug("No telegram.api sender: %s", e)

    # 2) core.providers.telegram
    try:
        import importlib
        mod = importlib.import_module("core.providers.telegram")
        for name in ("send_telegram", "send_message", "send_telegram_message"):
            fn = getattr(mod, name, None)
            if callable(fn):
                def _s2(text: str, _fn=fn) -> bool:
                    try:
                        _fn(text)
                        return True
                    except Exception as e:
                        log.debug("core.providers.telegram.%s failed: %s", name, e)
                        return False
                return _s2
    except Exception as e:
        log.debug("No core.providers.telegram sender: %s", e)

    # 3) core.providers.alerts
    try:
        import importlib
        mod = importlib.import_module("core.providers.alerts")
        for name in ("send_telegram", "send_message", "send_telegram_message"):
            fn = getattr(mod, name, None)
            if callable(fn):
                def _s3(text: str, _fn=fn) -> bool:
                    try:
                        _fn(text)
                        return True
                    except Exception as e:
                        log.debug("core.providers.alerts.%s failed: %s", name, e)
                        return False
                return _s3
    except Exception as e:
        log.debug("No core.providers.alerts sender: %s", e)

    # Fallback: no-op
    def _noop(text: str) -> bool:
        log.warning("No alert sender available. Message not delivered:\n%s", text)
        return False

    return _noop

_SENDER = _discover_sender()

def _send(text: str) -> bool:
    """Single call-site used by all helpers below."""
    return _SENDER(text)

# ------------------ Public API ------------------

def notify_error(context: str, err: Exception | str) -> None:
    """
    Send an error alert (and log it). Accepts Exception or str.
    """
    if isinstance(err, Exception):
        txt = f"❌ Error in {context}: {err}\n```\n{traceback.format_exc(limit=2)}\n```"
    else:
        txt = f"❌ Error in {context}: {err}"
    log.error(txt)
    try:
        _send(txt)
    except Exception as send_err:
        log.error("Failed to send error notification: %s", send_err)

def notify_alert(text: str) -> None:
    """
    Send a generic alert (pump/dump, guard, discovery, etc.).
    """
    msg = f"⚠️ {text}" if not text.startswith(("⚠️", "🚀", "🔔", "🔻", "🟢", "🟠")) else text
    log.warning(msg)
    try:
        _send(msg)
    except Exception as send_err:
        log.error("Failed to send alert notification: %s", send_err)

# Legacy alias (για παλιό κώδικα που ίσως σε φωνάζει με αυτό το όνομα)
def send_telegram_message(text: str) -> None:
    try:
        _send(text)
    except Exception as e:
        log.error("send_telegram_message failed: %s", e)

# Optional: allow runtime override with a custom sink (e.g., during tests)
def set_custom_sender(fn: Callable[[str], bool] | None) -> None:
    global _SENDER
    _SENDER = fn if callable(fn) else _discover_sender()
