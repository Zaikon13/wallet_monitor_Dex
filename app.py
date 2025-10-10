# app.py — FastAPI Telegram webhook using your real modules
from __future__ import annotations
import os
import logging
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, Request, HTTPException

# ΜΟΝΟ αυτό υπάρχει σίγουρα στο repo σου:
from core.holdings import get_wallet_snapshot  # <- δικό σου (επιστρέφει dict)  # :contentReference[oaicite:0]{index=0}

app = FastAPI(title="Cronos DeFi Sentinel — Telegram Webhook (prod)")

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
if not TELEGRAM_BOT_TOKEN:
    logging.warning("Missing TELEGRAM_BOT_TOKEN env — fallback sender will not work.")

def _fallback_send_message(text: str, chat_id: Optional[int]) -> None:
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
    # Δεν βασιζόμαστε σε telegram.api helper (στο repo είναι κενό)  # :contentReference[oaicite:1]{index=1}
    _fallback_send_message(text, chat_id)

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "wallet_monitor_Dex", "status": "running"}

def _fmt_money(x: str) -> str:
    try:
        # κρατάμε 2 δεκαδικά για USD, βγάζουμε τυχόν επιστημονική μορφή
        from decimal import Decimal
        d = Decimal(str(x))
        return f"{d.quantize(Decimal('0.01')):,}"
    except Exception:
        return str(x)

def _fmt_price(x: str) -> str:
    try:
        from decimal import Decimal
        d = Decimal(str(x))
        # μέχρι 8 δεκαδικά για price
        q = d.quantize(Decimal("0.00000001"))
        return f"{q:,}"
    except Exception:
        return str(x)

def _format_holdings_text(snapshot: Dict[str, Any]) -> str:
    assets: List[Dict[str, Any]] = snapshot.get("assets", []) or []
    totals: Dict[str, Any] = snapshot.get("totals", {}) or {}
    if not assets:
        return "📦 Holdings\n— Δεν βρέθηκαν assets."

    lines = ["📦 Holdings"]
    for a in assets:
        sym = a.get("symbol", "?")
        amt = a.get("amount", "0")
        px = a.get("price_usd", "0")
        val = a.get("value_usd", "0")
        # amount όπως είναι (το core/holdings επιστρέφει stringified Decimal)  # :contentReference[oaicite:2]{index=2}
        lines.append(f"• {sym}: {amt}  @ ${_fmt_price(px)}  (= ${_fmt_money(val)})")

    if totals:
        tv = _fmt_money(totals.get("value_usd", "0"))
        tc = _fmt_money(totals.get("cost_usd", "0"))
        tu = _fmt_money(totals.get("u_pnl_usd", "0"))
        tp = totals.get("u_pnl_pct", "0")
        try:
            from decimal import Decimal
            tp = str(Decimal(str(tp)).quantize(Decimal('0.01')))
        except Exception:
            tp = str(tp)
        lines.append("")
        lines.append(f"Σύνολα:  V=${tv} | C=${tc} | uPnL=${tu} ({tp}%)")
    return "\n".join(lines)

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
    try:
        snap = get_wallet_snapshot()  # φέρνει balances, prices, totals από τα ΚΑΝΟΝΙΚΑ modules  # :contentReference[oaicite:3]{index=3}
        return _format_holdings_text(snap)
    except Exception as e:
        logging.exception("Failed to build /holdings: %s", e)
        return "⚠️ Σφάλμα κατά τη δημιουργία των holdings (δες logs)."

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
