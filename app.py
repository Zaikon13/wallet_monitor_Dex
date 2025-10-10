# app.py — FastAPI Telegram webhook (prod) with MTM formatting + discovery merge
from __future__ import annotations
import os
import logging
from typing import Optional, Dict, Any, List

import requests
from fastapi import FastAPI, Request, HTTPException
from decimal import Decimal, InvalidOperation

from core.holdings import get_wallet_snapshot          # base snapshot (balances/prices/totals if διαθέσιμα)
from core.augment import augment_with_discovered_tokens # merge με discovery χωρίς να αλλάξουμε holdings.py
from core.discovery import discover_tokens_for_wallet

app = FastAPI(title="Cronos DeFi Sentinel — Telegram Webhook (prod)")

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").strip()

if not TELEGRAM_BOT_TOKEN:
    logging.warning("Missing TELEGRAM_BOT_TOKEN env — Telegram replies will not work.")
if not WALLET_ADDRESS:
    logging.warning("Missing WALLET_ADDRESS env — discovery merge will be disabled.")

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
    _fallback_send_message(text, chat_id)

@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "wallet_monitor_Dex", "status": "running"}

# ---------- formatting helpers ----------
def _to_dec(x: Any) -> Optional[Decimal]:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None

def _fmt_amt(x: Any, places: int = 4) -> str:
    d = _to_dec(x)
    if d is None:
        return str(x)
    q = d.quantize(Decimal("1." + "0"*places)) if places > 0 else d.quantize(Decimal("1"))
    return f"{q:,}"

def _fmt_price(x: Any) -> str:
    d = _to_dec(x)
    if d is None:
        return str(x)
    q = d.quantize(Decimal("0.000001"))
    return f"{q:,}"

def _fmt_money(x: Any) -> str:
    d = _to_dec(x)
    if d is None:
        return str(x)
    q = d.quantize(Decimal("0.01"))
    return f"{q:,}"

def _format_holdings_text(snapshot: Dict[str, Any]) -> str:
    assets: List[Dict[str, Any]] = snapshot.get("assets", []) or []
    totals: Dict[str, Any] = snapshot.get("totals", {}) or {}

    lines = ["💼 Wallet Assets (MTM)"]
    if not assets:
        lines.append("— Δεν βρέθηκαν assets.")
    else:
        for a in assets:
            sym = a.get("symbol", "?")
            amt = a.get("amount", "0")
            px  = a.get("price_usd", None)
            val = a.get("value_usd", None)
            lines.append(f"• {sym}: {_fmt_amt(amt, 4)} @ ${_fmt_price(px or 0)} = ${_fmt_money(val or 0)}")

    lines.append("")
    tv = totals.get("value_usd")
    tc = totals.get("cost_usd")
    tu = totals.get("u_pnl_usd")
    tp = totals.get("u_pnl_pct")

    lines.append(f"Σύνολο: ${_fmt_money(tv) if tv is not None else '—'}")
    if tu is not None:
        tp_txt = f"{_fmt_amt(tp, 2)}%" if tp is not None else "—"
        lines.append(f"Unrealized PnL (open): ${_fmt_money(tu)} ({tp_txt})")
    else:
        lines.append("Unrealized PnL (open): —")

    if assets:
        lines.append("\nQuantities snapshot (runtime):")
        for a in sorted(assets, key=lambda x: x.get("symbol", "")):
            lines.append(f"  – {a.get('symbol','?')}: {_fmt_amt(a.get('amount','0'), 4)}")
    return "\n".join(lines)

# ---------- commands ----------
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
        snap = get_wallet_snapshot()
        # Προαιρετικό merge με auto-discovery (αν έχουμε WALLET_ADDRESS)
        if WALLET_ADDRESS:
            snap = augment_with_discovered_tokens(snap, wallet_address=WALLET_ADDRESS)
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
    if cmd == "/scan":
        return _handle_scan(WALLET_ADDRESS)
    if cmd == "/rescan":
        return _handle_rescan(WALLET_ADDRESS)
    return "🤖 Δεν αναγνωρίζω την εντολή. Δοκίμασε /help."

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
    
def _handle_scan(wallet_address: str) -> str:
    if not wallet_address:
        return "⚠️ Δεν έχει οριστεί WALLET_ADDRESS στο περιβάλλον."
    try:
        toks = discover_tokens_for_wallet(wallet_address)
        if not toks:
            return "🔍 Δεν βρέθηκαν ERC-20 tokens με θετικό balance (ή δεν βρέθηκαν μεταφορές στο lookback)."
        lines = ["🔍 Discovery results:"]
        for t in toks:
            sym = t.get("symbol","?"); addr = t.get("address","?"); amt = t.get("amount","0")
            dec = t.get("decimals","?")
            lines.append(f"• {sym}  ({addr})  amount={amt}  decimals={dec}")
        return "\n".join(lines)
    except Exception as e:
        import traceback; traceback.print_exc()
        return f"⚠️ Σφάλμα στο discovery: {e}"

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
