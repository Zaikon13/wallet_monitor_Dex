# app.py
# FastAPI entrypoint για το Cronos DeFi Sentinel bot (Telegram webhook + commands)
from __future__ import annotations
import os
import logging
from typing import Optional
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import api as tg_api
from telegram.formatters import format_holdings
from core.holdings import get_wallet_snapshot
from core.augment import augment_with_discovered_tokens
from core.discovery import discover_tokens_for_wallet

# --------------------------------------------------
# Configuration
# --------------------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0")) or None
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
APP_URL = os.getenv("APP_URL")  # optional if needed for webhook set
EOD_TIME = os.getenv("EOD_TIME", "23:59")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI(title="Cronos DeFi Sentinel", version="1.0")

# --------------------------------------------------
# Helper: Safe Telegram message splitter
# --------------------------------------------------
def _fallback_send_message(text: str, chat_id: Optional[int] = None):
    """Basic fallback using telegram.api.send_telegram_message"""
    try:
        tg_api.send_telegram_message(text, chat_id=chat_id)
    except Exception as e:
        logging.exception(f"Telegram sendMessage failed: {e}")

def _send_long_text(text: str, chat_id: Optional[int], chunk: int = 3500) -> None:
    """Split μεγάλα μηνύματα (~>4096 chars) σε κομμάτια και τα στέλνει διαδοχικά."""
    if not text:
        return
    t = text
    while len(t) > chunk:
        cut = t.rfind("\n", 0, chunk)
        if cut == -1:
            cut = chunk
        part = t[:cut]
        _fallback_send_message(part, chat_id)
        t = t[cut:]
    _fallback_send_message(t, chat_id)

def send_message(text: str, chat_id: Optional[int] = None) -> None:
    """Main send wrapper: ασφαλές split μεγάλων μηνυμάτων"""
    _send_long_text(text, chat_id)

# --------------------------------------------------
# Command Handlers
# --------------------------------------------------
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
        "• /start — βασικές οδηγίες"
    )

def _handle_scan(wallet_address: str) -> str:
    toks = discover_tokens_for_wallet(wallet_address)
    if not toks:
        return "🔍 Δεν βρέθηκαν ERC-20 tokens με θετικό balance (ή δεν βρέθηκαν μεταφορές στο lookback)."
    lines = ["🔍 Εντοπίστηκαν tokens:"]
    for t in toks:
        sym = t.get("symbol", "?")
        amt = t.get("amount", "0")
        addr = t.get("address", "")
        lines.append(f"• {sym}: {amt} ({addr})")
    return "\n".join(lines)

def _handle_rescan(wallet_address: str) -> str:
    if not wallet_address:
        return "⚠️ Δεν έχει οριστεί WALLET_ADDRESS στο περιβάλλον."
    toks = discover_tokens_for_wallet(wallet_address)
    if not toks:
        return "🔁 Rescan ολοκληρώθηκε — δεν βρέθηκαν ERC-20 με θετικό balance."
    lines = ["🔁 Rescan ολοκληρώθηκε — βρέθηκαν:"]
    for t in toks:
        lines.append(f"• {t.get('symbol','?')} ({t.get('address','?')}), amount={t.get('amount','0')}")
    return "\n".join(lines)

def _handle_holdings(wallet_address: str) -> str:
    try:
        snap = get_wallet_snapshot(wallet_address)
        snap = augment_with_discovered_tokens(snap, wallet_address=wallet_address)
        return format_holdings(snap)
    except Exception as e:
        logging.exception("Failed to build /holdings")
        return "⚠️ Σφάλμα κατά τη δημιουργία των holdings."

# --------------------------------------------------
# Command dispatcher
# --------------------------------------------------
def _dispatch_command(text: str) -> str:
    if not text:
        return ""
    cmd = text.strip().split()[0].lower()
    if cmd == "/start":
        return _handle_start()
    if cmd == "/help":
        return _handle_help()
    if cmd == "/scan":
        return _handle_scan(WALLET_ADDRESS)
    if cmd == "/rescan":
        return _handle_rescan(WALLET_ADDRESS)
    if cmd == "/holdings":
        return _handle_holdings(WALLET_ADDRESS)
    return "❓ Άγνωστη εντολή. Δοκίμασε /help."

# --------------------------------------------------
# Webhook Endpoint (Telegram)
# --------------------------------------------------
@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
        message = update.get("message") or {}
        text = message.get("text", "")
        chat = message.get("chat", {})
        chat_id = chat.get("id", CHAT_ID)

        if text:
            reply = _dispatch_command(text)
            send_message(reply, chat_id)
    except Exception as e:
        logging.exception("Error handling Telegram webhook")
    return JSONResponse(content={"ok": True})

# --------------------------------------------------
# Health endpoint
# --------------------------------------------------
@app.get("/healthz")
async def healthz():
    return JSONResponse(content={"ok": True, "service": "wallet_monitor_Dex", "status": "running"})

# --------------------------------------------------
# Startup
# --------------------------------------------------
@app.on_event("startup")
async def on_startup():
    logging.info("✅ Cronos DeFi Sentinel started and is online.")
    try:
        send_message("✅ Cronos DeFi Sentinel started and is online.", CHAT_ID)
    except Exception:
        pass
