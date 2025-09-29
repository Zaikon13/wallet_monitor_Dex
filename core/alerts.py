# alerts.py
import logging
import traceback
from telegram.api import send_telegram as _send

def notify_error(context: str, err: Exception) -> None:
    """Send an error alert to Telegram and log it."""
    msg = (
        f"❌ Error in {context}: {err}\n"
        f"```\n{traceback.format_exc(limit=2)}\n```"
    )
    logging.error(msg)
    try:
        _send(msg)
    except Exception as send_err:
        logging.error(f"Failed to send error notification: {send_err}")

def notify_alert(text: str) -> None:
    """Send a generic alert to Telegram (e.g. price pump/dump)."""
    logging.warning(text)
    try:
        _send(f"⚠️ {text}")
    except Exception as send_err:
        logging.error(f"Failed to send alert notification: {send_err}")
