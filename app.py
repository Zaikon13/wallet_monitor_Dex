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
    """
    Στείλε μήνυμα στο Telegram χρησιμοποιώντας:
    1) telegram.api.send_telegram_message(text, chat_id)  (αν υπάρχει)
    2) telegram.api.send_telegram(text, chat_id)          (εναλλακτικό όνομα)
    3) raw HTTP fallback στο Bot API (αν αποτύχουν τα παραπάνω)
    """
    try:
        # 1) send_telegram_message(text, chat_id) (χωρίς keyword args)
        if hasattr(tg_api, "send_telegram_message"):
            return tg_api.send_telegram_message(text, chat_id)
        # 2) send_telegram(text, chat_id)
        if hasattr(tg_api, "send_telegram"):
            return tg_api.send_telegram(text, chat_id)
    except TypeError:
        # module δέχεται μόνο text (χωρίς chat_id)
        try:
            if hasattr(tg_api, "send_telegram_message"):
                return tg_api.send_telegram_message(text)
            if hasattr(tg_api, "send_telegram"):
                return tg_api.send_telegram(text)
        except Exception:
            pass
    except Exception as e:
        logging.exception(f"Telegram send via module failed: {e}")

    # 3) Raw HTTP fallback
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token or not chat_id:
        logging.error("No BOT_TOKEN or chat_id available for HTTP fallback.")
        return
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code >= 400:
            logging.error("Telegram HTTP fallback failed: %s %s", r.status_code, r.text)
    except Exception as e:
        logging.exception(f"Telegram HTTP fallback exception: {e}")

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
# Snapshot normalization helpers (για formatters)
# --------------------------------------------------
from decimal import Decimal, InvalidOperation

def _to_dec(x):
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return x

def _asset_as_dict(a):
    # Αν είναι ήδη dict, απλώς επέστρεψέ το
    if isinstance(a, dict):
        return a
    # Αν είναι list/tuple, χαρτογράφησέ το σε κλασική μορφή
    if isinstance(a, (list, tuple)):
        # Συνηθισμένη σειρά: symbol, amount, price_usd, value_usd
        fields = ["symbol", "amount", "price_usd", "value_usd"]
        d = {}
        for i, v in enumerate(a):
            key = fields[i] if i < len(fields) else f"extra_{i}"
            d[key] = v
        return d
    # Οτιδήποτε άλλο: τουλάχιστον δώσε symbol
    return {"symbol": str(a)}

def _normalize_snapshot_for_formatter(snap: dict) -> dict:
    out = dict(snap or {})
    assets = out.get("assets") or []
    out["assets"] = [_asset_as_dict(x) for x in assets]
    # Τυποποίηση σε Decimal όπου είναι εφικτό
    for a in out["assets"]:
        if "amount" in a:
            a["amount"] = _to_dec(a["amount"])
        if "price_usd" in a:
            a["price_usd"] = _to_dec(a["price_usd"])
        if "value_usd" in a:
            a["value_usd"] = _to_dec(a["value_usd"])
    return out

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
        # 1) Πάρε το βασικό snapshot
        snap = get_wallet_snapshot(wallet_address)
        # 2) Κάνε merge τα discovered tokens
        snap = augment_with_discovered_tokens(snap, wallet_address=wallet_address)
        # 3) Νοrmalize ώστε κάθε asset να είναι dict με ασφαλείς τύπους
        snap = _normalize_snapshot_for_formatter(snap)

        # ---- ΚΡΙΣΙΜΟ ----
        # Το format_holdings φαίνεται να περιμένει ΛΙΣΤΑ από assets (dicts).
        assets = snap.get("assets") or []
        if not isinstance(assets, list):
            assets = [assets]
        # Εξασφάλισε ότι κάθε στοιχείο είναι dict (διπλή ασφάλεια)
        assets = [_asset_as_dict(a) for a in assets]

        return format_holdings(assets)

    except Exception as e:
        logging.exception("Failed to build /holdings")

        # Fallback: απλό MTM print για να μη μένεις χωρίς απάντηση
        try:
            # προσπάθησε να εμφανίσεις ό,τι έχει ήδη υπολογιστεί
            lines = ["💼 Wallet Assets (MTM)"]
            snap = snap if isinstance(snap, dict) else {}
            assets = (snap.get("assets") or []) if isinstance(snap, dict) else []
            # μάζεψε βασικές γραμμές
            total = Decimal("0")
            for a in assets:
                d = _asset_as_dict(a)
                sym = str(d.get("symbol", "?"))
                amt = _to_dec(d.get("amount", 0)) or Decimal("0")
                px  = _to_dec(d.get("price_usd", 0)) or Decimal("0")
                val = _to_dec(d.get("value_usd", amt * px)) or Decimal("0")
                total += val
                lines.append(f"• {sym}: {amt} @ ${px} (= ${val})")
            lines.append(f"\nΣύνολο: ${total}")
            return "\n".join(lines) if assets else "⚠️ Σφάλμα κατά τη δημιουργία των holdings."
        except Exception:
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
