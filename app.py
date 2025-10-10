import os
import json
import logging
from typing import Optional

from fastapi import FastAPI, Request, HTTPException

# --- Optional: προσπαθώ να χρησιμοποιήσω το υπάρχον send_telegram_message ---
# Αν δεν υπάρχει/σπάει, κάνω fallback σε απευθείας κλήση Telegram API.
try:
    from telegram.api import send_telegram_message as _repo_send_message  # type: ignore
except Exception:  # pragma: no cover
    _repo_send_message = None

import requests

# Προαιρετικές εξαρτήσεις project αν υπάρχουν:
# - core.holdings.get_wallet_snapshot
# - telegram.formatters.format_holdings
# Χρησιμοποιώ "ήπια" imports ώστε να μη γίνει crash αν λείπουν·
# σε αυτή την περίπτωση απαντάω απλά ότι το command δεν είναι διαθέσιμο.
try:
    from core.holdings import get_wallet_snapshot  # type: ignore
except Exception:
    get_wallet_snapshot = None  # type: ignore

try:
    from telegram.formatters import format_holdings  # type: ignore
except Exception:
    format_holdings = None  # type: ignore

app = FastAPI(title="Cronos DeFi Sentinel — Telegram Webhook")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not TELEGRAM_BOT_TOKEN:
    logging.warning("Missing TELEGRAM_BOT_TOKEN env — fallback sender won't work.")

def _fallback_send_message(text: str, chat_id: Optional[int]) -> None:
    """Απευθείας κλήση στο Telegram sendMessage όταν δεν υπάρχει repo helper."""
    if not chat_id:
        logging.warning("No chat_id provided to fallback sender; message dropped.")
        return
    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN missing; cannot send Telegram message.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code >= 400:
            logging.error("Telegram sendMessage failed: %s %s", r.status_code, r.text)
    except Exception as e:  # pragma: no cover
        logging.exception("Telegram sendMessage exception: %s", e)

def send_message(text: str, chat_id: Optional[int]) -> None:
    """Ενιαίο API αποστολής — χρησιμοποιεί helper από το repo ή fallback HTTP."""
    if _repo_send_message:
        # Κάποια projects έχουν helper χωρίς chat_id (στέλνει στο προεπιλεγμένο CHAT_ID).
        # Προσπαθώ πρώτα με chat_id, αλλιώς πέφτω σε κλήση χωρίς chat_id.
        try:
            try:
                _repo_send_message(text, chat_id=chat_id)  # type: ignore[arg-type]
                return
            except TypeError:
                _repo_send_message(text)  # type: ignore[call-arg]
                return
        except Exception:
            logging.exception("repo send_telegram_message failed; using fallback…")
    _fallback_send_message(text, chat_id)

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "wallet_monitor_Dex", "status": "running"}

def _handle_holdings() -> str:
    if not (get_wallet_snapshot and format_holdings):
        return ("⚠️ Το /holdings δεν είναι έτοιμο σ’ αυτό το build.\n"
                "Λείπει είτε το core.holdings.get_wallet_snapshot είτε το telegram.formatters.format_holdings.")
    try:
        snap = get_wallet_snapshot()
        return format_holdings(snap)
    except Exception as e:
        logging.exception("Failed to build /holdings: %s", e)
        return "⚠️ Σφάλμα κατά τη δημιουργία των holdings."

def _handle_start() -> str:
    return (
        "👋 Γεια σου! Είμαι το Cronos DeFi Sentinel.\n\n"
        "Διαθέσιμες εντολές:\n"
        "• /holdings — snapshot χαρτοφυλακίου\n"
        "• /help — βοήθεια"
    )

def _handle_help() -> str:
    return (
        "ℹ️ Βοήθεια\n"
        "• /holdings — δείχνει τρέχον snapshot\n"
        "• /start — βασικές οδηγίες\n"
    )

def _dispatch_command(text: str) -> str:
    cmd = (text or "").strip().split()[0].lower()
    if cmd == "/start":
        return _handle_start()
    if cmd == "/help":
        return _handle_help()
    if cmd == "/holdings":
        return _handle_holdings()
    return "🤖 Δεν αναγνωρίζω την εντολή. Δοκίμασε /help."

@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Υποστηρίζουμε message & edited_message
    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}  # αγνοούμε update τύπου callback, join events κ.λπ.

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text: Optional[str] = message.get("text")

    if not text:
        # Αγνόησε μη-text μηνύματα ευγενικά.
        send_message("Μπορώ να απαντώ σε text commands. Δοκίμασε /help 🙂", chat_id)
        return {"ok": True}

    reply = _dispatch_command(text)
    send_message(reply, chat_id)
    return {"ok": True}
