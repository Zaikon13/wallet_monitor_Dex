# app.py — FastAPI Telegram webhook using your real modules
from __future__ import annotations
import os
import logging
from typing import Optional

import requests
from fastapi import FastAPI, Request, HTTPException

# Χρησιμοποιούμε ΤΑ ΚΑΝΟΝΙΚΑ modules σου
from core.holdings import get_wallet_snapshot, format_snapshot_lines  # <- δικά σου

# Προαιρετικά χρησιμοποιούμε το δικό σου helper αν υπάρχει chat_id-less send
try:
    from telegram.api import send_telegram as _repo_send  # sends to TELEGRAM_CHAT_ID (broadcast)
except Exception:
    _repo_send = None  # fallback σε direct HTTP προς Telegram με chat_id

app = FastAPI(title="Cronos DeFi Sentinel — Telegram Webhook (prod)")

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
if not TELEGRAM_BOT_TOKEN:
    logging.warning("Missing TELEGRAM_BOT_TOKEN env — fallback sender will not work.")

def _fallback_send_message(text: str, chat_id: Optional[int]) -> None:
    """Απευθείας sendMessage με chat_id όταν δεν καλεί ο repo helper."""
    if not chat_id:
        logging.warning("No chat_id provided to fallback sender; dropping message.")
        return
    if not TELEGRAM_BOT_TOKEN:
        logging.error("TELEGRAM_BOT_TOKEN missing; cannot send Telegram message.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code >= 400:
            logging.error("Telegram sendMessage failed: %s %s", r.status_code, r.text)
    except Exception:
        logging.exception("Telegram sendMessage exception")

def send_message(text: str, chat_id: Optional[int]) -> None:
    """
    Ενιαίο API: Αν υπάρχει repo helper (broadcast στο TELEGRAM_CHAT_ID) τον χρησιμοποιούμε,
    αλλιώς στέλνουμε απευθείας στο chat_id του αιτήματος.
    """
    if _repo_send:
        try:
            _repo_send(text)  # broadcast
            return
        except Exception:
            logging.exception("telegram.api.send_telegram failed; using fallback…")
    _fallback_send_message(text, chat_id)

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "wallet_monitor_Dex", "status": "running"}

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

def _handle_holdings() -> str:
    """
    Χτίζει snapshot με τα ΚΑΝΟΝΙΚΑ modules σου (RPC, pricing, etherscan-like),
    και το φορμάρει με format_snapshot_lines() από core/holdings.py.
    """
    try:
        snap = get_wallet_snapshot()
        return "📦 Holdings\n" + format_snapshot_lines(snap)
    except Exception as e:
        logging.exception("Failed to build /holdings: %s", e)
        return "⚠️ Σφάλμα κατά τη δημιουργία των holdings (check logs)."

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

    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text: Optional[str] = message.get("text")

    if not text:
        send_message("Μπορώ να απαντώ σε text commands. Δοκίμασε /help 🙂", chat_id)
        return {"ok": True}

    reply = _dispatch_command(text)
    send_message(reply, chat_id)
    return {"ok": True}
