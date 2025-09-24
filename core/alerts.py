# core/alerts.py
import logging
import traceback
from telegram.api import send_telegram_message

def notify_error(context: str, err: Exception):
    """Send an error alert to Telegram and log it."""
    msg = f"❌ Error in {context}: {err}\n```\n{traceback.format_exc(limit=2)}\n```"
    logging.error(msg)
    try:
        send_telegram_message(msg)
    except Exception as send_err:
        logging.error(f"Failed to send error notification: {send_err}")

def notify_alert(text: str):
    """Send a generic alert to Telegram (e.g. price pump/dump)."""
    logging.warning(text)
    try:
        send_telegram_message(f"⚠️ {text}")
    except Exception as send_err:
        logging.error(f"Failed to send alert notification: {send_err}")
