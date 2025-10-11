# app.py — FastAPI entrypoint για το Cronos DeFi Sentinel (Telegram webhook + commands)
from __future__ import annotations

import os
import re
import json
import logging
from typing import Optional, Dict, Any, List, Callable
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation

from zoneinfo import ZoneInfo
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Telegram send helpers
from telegram import api as tg_api

# Core / repo imports
from core.holdings import get_wallet_snapshot
from core.pricing import get_spot_usd

# ΝΕΑ: Intraday Trades & PnL
from reports.trades import todays_trades, realized_pnl_today
from telegram.formatters import format_trades_table, format_pnl_today

# Προαιρετικό delegate σε υπάρχον command router αν υπάρχει
try:
    # Αν υπάρχει module με commands και COMMANDS / register
    from telegram import commands as tg_commands  # type: ignore
except Exception:  # module not found ή import error
    tg_commands = None

# --------------------------------------------------
# Configuration / ENV
# --------------------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0")) or None
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
APP_URL = os.getenv("APP_URL")  # αν χρειαστεί για webhook set
EOD_TIME = os.getenv("EOD_TIME", "23:59")
TZ = os.getenv("TZ", "Europe/Athens")
SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", "./data/snapshots")  # Railway: πιθανώς ephemeral

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI(title="Cronos DeFi Sentinel", version="1.2")

# --------------------------------------------------
# Telegram send (με ασφαλές split μεγάλων μηνυμάτων)
# --------------------------------------------------
def _fallback_send_message(text: str, chat_id: Optional[int] = None):
    """Στείλε μήνυμα στο Telegram με:
    1) telegram.api.send_telegram_message(text, chat_id)
    2) telegram.api.send_telegram(text, chat_id)
    3) raw HTTP fallback στο Bot API
    """
    try:
        if hasattr(tg_api, "send_telegram_message"):
            return tg_api.send_telegram_message(text, chat_id)
        if hasattr(tg_api, "send_telegram"):
            return tg_api.send_telegram(text, chat_id)
    except TypeError:
        # ορισμένα wrappers δέχονται μόνο text
        try:
            if hasattr(tg_api, "send_telegram_message"):
                return tg_api.send_telegram_message(text)
            if hasattr(tg_api, "send_telegram"):
                return tg_api.send_telegram(text)
        except Exception:
            pass
    except Exception as e:
        logging.exception(f"Telegram send via module failed: {e}")

    # HTTP fallback
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token or not chat_id:
        logging.error("No BOT_TOKEN or chat_id available for HTTP fallback.")
        return
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
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
    _send_long_text(text, chat_id or CHAT_ID)

# --------------------------------------------------
# Small utils / normalizers (κρατάμε το ίδιο μοτίβο με το repo)
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

def _env_bool(name: str, default: bool) -> bool:
    v = (os.getenv(name) or "").strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return default

def _env_dec(name: str, default: str) -> Decimal:
    try:
        return Decimal(os.getenv(name, default))
    except Exception:
        return Decimal(default)

def _now_local() -> datetime:
    return datetime.now(ZoneInfo(TZ))

# --------------------------------------------------
# Holdings helpers (όπως στο υπάρχον μοτίβο)
# --------------------------------------------------
def _filter_and_sort_assets(assets: list) -> tuple[list, int]:
    """ENV-based filtering για holdings.
    - HOLDINGS_HIDE_ZERO_PRICE (default True)
    - HOLDINGS_DUST_USD (default 0.05)
    - HOLDINGS_BLACKLIST_REGEX
    - HOLDINGS_LIMIT
    Majors δεν κόβονται λόγω price=0/dust. Αν δεν υπάρχει τιμή, γίνεται live spot lookup.
    """
    hide_zero = _env_bool("HOLDINGS_HIDE_ZERO_PRICE", True)
    dust_usd = _env_dec("HOLDINGS_DUST_USD", "0.05")
    limit = int(os.getenv("HOLDINGS_LIMIT", "40"))
    bl_re_pat = os.getenv(
        "HOLDINGS_BLACKLIST_REGEX",
        r"(?i)(claim|airdrop|promo|mistery|crowithknife|classic|button|ryoshi|ethena\.promo)",
    )
    bl_re = re.compile(bl_re_pat)

    majors = {"CRO", "WCRO", "USDT", "USDC", "WETH", "WBTC", "ADA", "SOL", "XRP", "SUI", "MATIC", "HBAR"}

    visible = []
    hidden = 0
    for a in assets:
        d = _asset_as_dict(a)
        sym = str(d.get("symbol", "?")).upper().strip()
        addr = d.get("address")
        price = _to_dec(d.get("price_usd", 0)) or Decimal("0")
        amt = _to_dec(d.get("amount", 0)) or Decimal("0")

        # Για majors χωρίς τιμή, προσπάθησε live spot
        if sym in majors and (price is None or price <= 0):
            try:
                px = get_spot_usd(sym, token_address=addr)
                price = _to_dec(px) or Decimal("0")
                d["price_usd"] = price
            except Exception:
                pass

        val = _to_dec(d.get("value_usd", 0))
        if val is None or val == 0:
            val = amt * (price or Decimal("0"))
        d["value_usd"] = val

        if bl_re.search(sym):
            hidden += 1
            continue

        if sym not in majors:
            if hide_zero and (price is None or price <= 0):
                hidden += 1
                continue
            if val is None or val < dust_usd:
                hidden += 1
                continue
        # majors: άφησέ τα να περάσουν

        d["symbol"] = sym
        d["amount"] = amt
        d["price_usd"] = price
        d["value_usd"] = val
        visible.append(d)

    visible.sort(key=lambda x: (x.get("value_usd") or Decimal("0")), reverse=True)

    if limit and len(visible) > limit:
        hidden += (len(visible) - limit)
        visible = visible[:limit]

    return visible, hidden

def _assets_list_to_mapping(assets: list) -> dict:
    """Μετατρέπει λίστα assets σε mapping {SYMBOL: data}, aggregating duplicates."""
    out: Dict[str, dict] = OrderedDict()
    for item in assets:
        d = _asset_as_dict(item)
        sym = str(d.get("symbol", "?")).upper().strip() or d.get("address", "")[:6].upper()
        amt = _to_dec(d.get("amount", 0)) or Decimal("0")
        px = _to_dec(d.get("price_usd", 0)) or Decimal("0")
        val = _to_dec(d.get("value_usd", 0))
        if val is None:
            val = amt * px

        if sym in out:
            prev = out[sym]
            prev_amt = _to_dec(prev.get("amount", 0)) or Decimal("0")
            new_amt = prev_amt + amt
            new_px = px if px > 0 else (_to_dec(prev.get("price_usd", 0)) or Decimal("0"))
            new_val = new_amt * new_px if new_px > 0 else \
                ((_to_dec(prev.get("value_usd", 0)) or Decimal("0")) + (val or Decimal("0")))
            prev.update(d)
            prev["amount"] = new_amt
            prev["price_usd"] = new_px
            prev["value_usd"] = new_val
        else:
            d["amount"] = amt
            d["price_usd"] = px
            d["value_usd"] = val
            out[sym] = d
    return out

# --------------------------------------------------
# Intraday commands (ΝΕΑ): /trades & /pnl today
# --------------------------------------------------
def _handle_trades(symbol: Optional[str] = None) -> str:
    try:
        syms = [symbol.upper()] if symbol else None
        trades = todays_trades(syms)
        return format_trades_table(trades)
    except Exception:
        logging.exception("Failed to build /trades")
        return "⚠️ Σφάλμα κατά τη δημιουργία λίστας σημερινών συναλλαγών."

def _handle_pnl_today(symbol: Optional[str] = None) -> str:
    """ /pnl today [SYM] — realized PnL μόνο για σήμερα (FIFO ανά σύμβολο) """
    try:
        summary = realized_pnl_today()
        text = format_pnl_today(summary)
        if symbol:
            sym = symbol.upper().strip()
            # Ελαφρύ φίλτρο πάνω στο formatted text
            lines = []
            for line in text.splitlines():
                if line.startswith("- "):
                    if line[2:].upper().startswith(sym):
                        lines.append(line)
                else:
                    lines.append(line)  # headers/totals
            if len(lines) > 1:
                return "\n".join(lines)
        return text
    except Exception:
        logging.exception("Failed to compute realized PnL (today)")
        return "⚠️ Σφάλμα κατά τον υπολογισμό του realized PnL για σήμερα."

# --------------------------------------------------
# Command dispatcher
# --------------------------------------------------
def _delegate_to_existing_commands(cmd: str, args: List[str]) -> Optional[str]:
    """Αν υπάρχει telegram.commands με COMMANDS ή register/dispatch, κάνε delegate."""
    if not tg_commands:
        return None

    # COMMANDS dict (common pattern)
    try:
        COMMANDS = getattr(tg_commands, "COMMANDS", None)
        if isinstance(COMMANDS, dict) and cmd in COMMANDS:
            func = COMMANDS[cmd]
            if callable(func):
                arg_str = " ".join(args)
                return func(arg_str)
    except Exception:
        logging.exception("COMMANDS delegate failed")

    # generic dispatch(text) style
    try:
        dispatch = getattr(tg_commands, "dispatch", None)
        if callable(dispatch):
            return dispatch(" ".join([cmd] + args))
    except Exception:
        logging.exception("dispatch delegate failed")

    # register-based (pattern, func) holder
    try:
        registered = getattr(tg_commands, "REGISTERED", None)
        if isinstance(registered, dict) and cmd in registered:
            func = registered[cmd]
            if callable(func):
                return func(" ".join(args))
    except Exception:
        logging.exception("REGISTERED delegate failed")

    return None

def _dispatch_command(text: str) -> str:
    """Απλός parser: πιάνει τα νέα commands μας, αλλιώς delegate στα υπάρχοντα."""
    if not text:
        return "⚠️ Άδειο μήνυμα."

    parts = text.strip().split()
    cmd = parts[0]
    args = parts[1:]

    # ΝΕΑ: /trades
    if cmd == "/trades":
        sym = args[0] if args else None
        return _handle_trades(sym)

    # ΝΕΑ: /pnl today
    if cmd == "/pnl" and args and args[0].lower() == "today":
        sym = args[1] if len(args) > 1 else None
        return _handle_pnl_today(sym)

    # Διαφορετικά, προσπάθησε delegate στο υπάρχον σύστημα εντολών (αν υπάρχει)
    delegated = _delegate_to_existing_commands(cmd, args)
    if delegated is not None:
        return delegated

    # Fallback help
    return (
        "Διαθέσιμες εντολές:\n"
        "• /trades [SYM] — σημερινές συναλλαγές (τοπική TZ)\n"
        "• /pnl today [SYM] — σημερινό realized PnL (FIFO)\n"
        "(Άλλες εντολές εξυπηρετούνται από το υπάρχον command router, αν είναι διαθέσιμο.)"
    )

# --------------------------------------------------
# FastAPI routes
# --------------------------------------------------
@app.get("/healthz")
async def healthz():
    return {"ok": True, "ts": _now_local().isoformat()}

@app.post(f"/telegram/webhook")
async def telegram_webhook(req: Request):
    """Webhook endpoint για Telegram bot."""
    try:
        payload = await req.json()
    except Exception:
        payload = {}

    # Βρες chat_id & text
    chat_id = None
    text = None
    try:
        msg = payload.get("message") or payload.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id") or CHAT_ID
        text = msg.get("text") or ""
    except Exception:
        logging.exception("Invalid Telegram payload shape")
        text = ""

    reply = _dispatch_command(text)
    try:
        send_message(reply, chat_id=chat_id)
    except Exception:
        logging.exception("Failed to send Telegram reply")

    return JSONResponse({"ok": True})

@app.get("/")
async def root():
    return {"name": "Cronos DeFi Sentinel", "version": "1.2", "tz": TZ}

