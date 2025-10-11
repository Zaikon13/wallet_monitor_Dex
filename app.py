# app.py
# FastAPI entrypoint για το Cronos DeFi Sentinel bot (Telegram webhook + commands)
from __future__ import annotations

import os
import re
import logging
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import api as tg_api
from telegram.formatters import format_holdings
from core.holdings import get_wallet_snapshot
from core.augment import augment_with_discovered_tokens
from core.discovery import discover_tokens_for_wallet
from core.pricing import get_spot_usd

from decimal import Decimal, InvalidOperation
from collections import OrderedDict

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
        if hasattr(tg_api, "send_telegram_message"):
            return tg_api.send_telegram_message(text, chat_id)
        if hasattr(tg_api, "send_telegram"):
            return tg_api.send_telegram(text, chat_id)
    except TypeError:
        try:
            if hasattr(tg_api, "send_telegram_message"):
                return tg_api.send_telegram_message(text)
            if hasattr(tg_api, "send_telegram"):
                return tg_api.send_telegram(text)
        except Exception:
            pass
    except Exception as e:
        logging.exception(f"Telegram send via module failed: {e}")

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
def _to_dec(x):
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return x

def _asset_as_dict(a):
    if isinstance(a, dict):
        return a
    if isinstance(a, (list, tuple)):
        fields = ["symbol", "amount", "price_usd", "value_usd"]
        d = {}
        for i, v in enumerate(a):
            key = fields[i] if i < len(fields) else f"extra_{i}"
            d[key] = v
        return d
    return {"symbol": str(a)}

def _normalize_snapshot_for_formatter(snap: dict) -> dict:
    out = dict(snap or {})
    assets = out.get("assets") or []
    out["assets"] = [_asset_as_dict(x) for x in assets]
    for a in out["assets"]:
        if "amount" in a:
            a["amount"] = _to_dec(a["amount"])
        if "price_usd" in a:
            a["price_usd"] = _to_dec(a["price_usd"])
        if "value_usd" in a:
            a["value_usd"] = _to_dec(a["value_usd"])
    return out

# --------------------------------------------------
# ENV-driven filters/sorting for holdings/rescan
# --------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if v in ("1","true","yes","y","on"): return True
    if v in ("0","false","no","n","off"): return False
    return default

def _env_dec(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except Exception:
        return Decimal(default)

def _filter_and_sort_assets(assets: list) -> tuple[list, int]:
    """
    Εφαρμόζει φίλτρα/ταξινόμηση σύμφωνα με env:
    - HOLDINGS_HIDE_ZERO_PRICE (default True)
    - HOLDINGS_DUST_USD (default 0.05)
    - HOLDINGS_BLACKLIST_REGEX (για spammy ονόματα)
    - HOLDINGS_LIMIT (πόσες γραμμές να δείξουμε)
    Επιστρέφει: (visible_assets, hidden_count)
    """
    hide_zero = _env_bool("HOLDINGS_HIDE_ZERO_PRICE", True)
    dust_usd  = _env_dec("HOLDINGS_DUST_USD", "0.05")
    limit     = int(os.getenv("HOLDINGS_LIMIT", "40"))
    bl_re_pat = os.getenv("HOLDINGS_BLACKLIST_REGEX", r"(?i)(claim|airdrop|promo|mistery|crowithknife|classic|button|ryoshi|ethena\.promo)")
    bl_re = re.compile(bl_re_pat)

    visible = []
    hidden  = 0
    whitelist_zero_price = {"USDT","USDC","WCRO","CRO","WETH","WBTC","ADA","SOL","XRP","SUI","MATIC"}

    for a in assets:
        d = _asset_as_dict(a)
        sym = str(d.get("symbol","?")).upper().strip()
        price = _to_dec(d.get("price_usd", 0)) or Decimal("0")
        val   = _to_dec(d.get("value_usd", 0))
        if val is None or isinstance(val, (str, float, int)):
            amt = _to_dec(d.get("amount", 0)) or Decimal("0")
            val = amt * price

        if bl_re.search(sym):
            hidden += 1
            continue
        if hide_zero and price <= 0 and sym not in whitelist_zero_price:
            hidden += 1
            continue
        if val is None or val < dust_usd:
            hidden += 1
            continue

        d["price_usd"] = price
        d["value_usd"] = val
        visible.append(d)

    visible.sort(key=lambda x: (x.get("value_usd") or Decimal("0")), reverse=True)

    if limit and len(visible) > limit:
        hidden += (len(visible) - limit)
        visible = visible[:limit]

    return visible, hidden

def _assets_list_to_mapping(assets: list) -> dict:
    """
    Μετατρέπει λίστα από assets (dicts) σε mapping {SYMBOL: data}.
    Αν βρεθεί διπλό symbol, τα κάνει aggregate (amount) και
    ανανεώνει price/value λογικά.
    """
    out: dict[str, dict] = OrderedDict()
    for item in assets:
        d = _asset_as_dict(item)
        sym = str(d.get("symbol", "?")).upper().strip() or d.get("address", "")[:6].upper()
        amt = _to_dec(d.get("amount", 0)) or Decimal("0")
        px  = _to_dec(d.get("price_usd", 0)) or Decimal("0")
        val = _to_dec(d.get("value_usd", 0))
        if val is None:
            val = amt * px

        if sym in out:
            prev = out[sym]
            prev_amt = _to_dec(prev.get("amount", 0)) or Decimal("0")
            # aggregate ποσότητα
            new_amt = prev_amt + amt
            # κράτα πιο “πρόσφατη”/μη μηδενική τιμή
            new_px  = px if px > 0 else (_to_dec(prev.get("price_usd", 0)) or Decimal("0"))
            # value συνεπές με amount*price, αλλιώς άθροισε values
            new_val = new_amt * new_px if new_px > 0 else \
                      ((_to_dec(prev.get("value_usd", 0)) or Decimal("0")) + (val or Decimal("0")))
            prev.update(d)
            prev["amount"]    = new_amt
            prev["price_usd"] = new_px
            prev["value_usd"] = new_val
        else:
            d["amount"]    = amt
            d["price_usd"] = px
            d["value_usd"] = val
            out[sym] = d
    return out

# --------------------------------------------------
# Command Handlers
# --------------------------------------------------
def _handle_start() -> str:
    return (
        "👋 Γεια σου! Είμαι το Cronos DeFi Sentinel.\n\n"
        "Διαθέσιμες εντολές:\n"
        "• /holdings — snapshot χαρτοφυλακίου (με φίλτρα & ταξινόμηση)\n"
        "• /scan — ωμή λίστα tokens από ανακάλυψη (χωρίς φίλτρα)\n"
        "• /rescan — πλήρης επανεύρεση & εμφάνιση (με φίλτρα)\n"
        "• /help — βοήθεια"
    )

def _handle_help() -> str:
    return (
        "ℹ️ Βοήθεια\n"
        "• /holdings — δείχνει τρέχον snapshot (MTM με τιμές, φιλτραρισμένο)\n"
        "• /scan — ωμή λίστα tokens που βρέθηκαν (amount + address)\n"
        "• /rescan — ξανά σκανάρισμα & παρουσίαση όπως το /holdings\n"
        "• /start — βασικές οδηγίες"
    )

def _handle_scan(wallet_address: str) -> str:
    toks = discover_tokens_for_wallet(wallet_address)
    if not toks:
        return "🔍 Δεν βρέθηκαν ERC-20 tokens με θετικό balance (ή δεν βρέθηκαν μεταφορές στο lookback)."
    lines = ["🔍 Εντοπίστηκαν tokens (raw):"]
    for t in toks:
        sym = t.get("symbol", "?")
        amt = t.get("amount", "0")
        addr = t.get("address", "")
        lines.append(f"• {sym}: {amt} ({addr})")
    return "\n".join(lines)

def _enrich_with_prices(tokens: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Γέμισε price/value για tokens ώστε να περάσουν φίλτρα/ταξινόμηση."""
    out: List[Dict[str, Any]] = []
    for t in tokens:
        d = _asset_as_dict(t)
        sym = str(d.get("symbol","?")).upper()
        addr = d.get("address")
        amt  = _to_dec(d.get("amount", 0)) or Decimal("0")
        price = get_spot_usd(sym, token_address=addr)
        price_dec = _to_dec(price) or Decimal("0")
        value = amt * price_dec
        d["price_usd"] = price_dec
        d["value_usd"] = value
        out.append(d)
    return out

def _handle_rescan(wallet_address: str) -> str:
    if not wallet_address:
        return "⚠️ Δεν έχει οριστεί WALLET_ADDRESS στο περιβάλλον."
    toks = discover_tokens_for_wallet(wallet_address)
    if not toks:
        return "🔁 Rescan ολοκληρώθηκε — δεν βρέθηκαν ERC-20 με θετικό balance."

    # Εμπλούτισε με τιμές για να λειτουργήσουν φίλτρα/ταξινομήσεις
    enriched = _enrich_with_prices(toks)
    cleaned, hidden_count = _filter_and_sort_assets(enriched)

    if not cleaned:
        return "🔁 Rescan ολοκληρώθηκε — δεν υπάρχει κάτι αξιοσημείωτο να εμφανιστεί (όλα φιλτραρίστηκαν ως spam/zero/dust)."

    # Σύντομη λίστα για Telegram, με value
    lines = ["🔁 Rescan (top, filtered):"]
    for d in cleaned:
        sym = d.get("symbol","?")
        amt = d.get("amount","0")
        px  = d.get("price_usd", Decimal("0"))
        val = d.get("value_usd", Decimal("0"))
        addr = d.get("address","")
        lines.append(f"• {sym}: {amt} @ ${px} (= ${val})  ({addr})")

    if hidden_count:
        lines.append(f"\n(…και άλλα {hidden_count} κρυμμένα: spam/zero-price/dust)")

    return "\n".join(lines)

def _handle_holdings(wallet_address: str) -> str:
    try:
        snap = get_wallet_snapshot(wallet_address)
        snap = augment_with_discovered_tokens(snap, wallet_address=wallet_address)
        snap = _normalize_snapshot_for_formatter(snap)

        assets = snap.get("assets") or []
        assets = [_asset_as_dict(a) for a in assets]

        # φίλτρα/ταξινόμηση για καθαρή προβολή
        cleaned, hidden_count = _filter_and_sort_assets(assets)

        # format_holdings περιμένει dict: {symbol: data}
        mapping = _assets_list_to_mapping(cleaned)

        body = format_holdings(mapping)
        if hidden_count:
            body += f"\n\n(…και άλλα {hidden_count} κρυμμένα: spam/zero-price/dust)"
        return body

    except Exception:
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
