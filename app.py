# app.py
# FastAPI entrypoint για το Cronos DeFi Sentinel bot (Telegram webhook + commands)
from __future__ import annotations

import os
import re
import json
import logging
from typing import Optional, List, Dict, Any
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import api as tg_api
# Προαιρετικά κρατάμε διαθέσιμο το external formatter (δεν το χρησιμοποιούμε εδώ)
# from telegram.formatters import format_holdings as _unused_external_formatter

from core.holdings import get_wallet_snapshot
from core.augment import augment_with_discovered_tokens
from core.discovery import discover_tokens_for_wallet
from core.pricing import get_spot_usd

# --- ΝΕΑ για Phase 2: intraday trades & realized PnL (today) ---
from reports.trades import todays_trades, realized_pnl_today
from telegram.formatters import format_trades_table, format_pnl_today

from decimal import Decimal, InvalidOperation

# --------------------------------------------------
# Configuration
# --------------------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0")) or None
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
APP_URL = os.getenv("APP_URL")  # optional, αν το χρειαστείς για webhook set
EOD_TIME = os.getenv("EOD_TIME", "23:59")
TZ = os.getenv("TZ", "Europe/Athens")
SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", "./data/snapshots")  # Railway: ephemeral ανά deploy, το ξέρουμε

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI(title="Cronos DeFi Sentinel", version="1.1")

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
        # ορισμένα modules περιμένουν μόνο text
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
# Snapshot normalization helpers (για φίλτρα/formatting)
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

    Κανόνες για majors (CRO, WCRO, κ.λπ.):
    - Αν price==0, δοκίμασε live get_spot_usd πριν φιλτράρεις
    - ΜΗΝ εφαρμόζεις dust filter στα majors
    """
    hide_zero = _env_bool("HOLDINGS_HIDE_ZERO_PRICE", True)
    dust_usd  = _env_dec("HOLDINGS_DUST_USD", "0.05")
    limit     = int(os.getenv("HOLDINGS_LIMIT", "40"))
    bl_re_pat = os.getenv("HOLDINGS_BLACKLIST_REGEX", r"(?i)(claim|airdrop|promo|mistery|crowithknife|classic|button|ryoshi|ethena\.promo)")
    bl_re = re.compile(bl_re_pat)

    # Majors = δεν κόβονται από dust· αν λείπει τιμή, κάνε live lookup
    majors = {"USDT","USDC","WCRO","CRO","WETH","WBTC","ADA","SOL","XRP","SUI","MATIC","HBAR"}

    visible = []
    hidden  = 0

    for a in assets:
        d = _asset_as_dict(a)
        sym = str(d.get("symbol","?")).upper().strip()
        addr = d.get("address")
        price = _to_dec(d.get("price_usd", 0)) or Decimal("0")
        amt   = _to_dec(d.get("amount", 0)) or Decimal("0")

        # Αν major και price==0 → προσπάθησε live spot lookup
        if sym in majors and (price is None or price <= 0):
            try:
                px_live = get_spot_usd(sym, token_address=addr)
                price = _to_dec(px_live) or Decimal("0")
            except Exception:
                # αν αποτύχει, κρατάμε 0 αλλά δεν θα το κόψουμε λόγω dust
                pass

        # Υπολόγισε value (αν λείπει)
        val = _to_dec(d.get("value_usd", 0))
        if val is None or isinstance(val, (str, float, int)):
            val = amt * (price or Decimal("0"))

        # blacklist by symbol (spam/claims)
        if bl_re.search(sym):
            hidden += 1
            continue

        # zero-price φίλτρο: κόψε ΜΟΝΟ αν δεν είναι major
        if hide_zero and (price is None or price <= 0) and sym not in majors:
            hidden += 1
            continue

        # dust φίλτρο: κόψε ΜΟΝΟ αν δεν είναι major
        if sym not in majors:
            if val is None or val < dust_usd:
                hidden += 1
                continue

        # πέρασε
        d["price_usd"] = price
        d["value_usd"] = val
        visible.append(d)

    # sort by USD value desc
    visible.sort(key=lambda x: (_to_dec(x.get("value_usd")) or Decimal("0")), reverse=True)

    # cap results
    if limit and len(visible) > limit:
        hidden += (len(visible) - limit)
        visible = visible[:limit]

    return visible, hidden

def _assets_list_to_mapping(assets: list) -> dict:
    """
    Μετατρέπει λίστα από assets (dicts) σε mapping {SYMBOL: data}.
    Αν βρεθεί διπλό symbol, κάνει aggregate (amount) και ενημερώνει price/value.
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
            new_amt = prev_amt + amt
            new_px  = px if px > 0 else (_to_dec(prev.get("price_usd", 0)) or Decimal("0"))
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
# Snapshots storage (multi per day) + PnL helpers
# --------------------------------------------------
def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def _now_local() -> datetime:
    return datetime.now(ZoneInfo(TZ))

def _today_str() -> str:
    return _now_local().date().isoformat()  # YYYY-MM-DD

def _now_stamp() -> str:
    # YYYY-MM-DD_HHMM (π.χ. 2025-10-11_0945)
    dt = _now_local()
    return f"{dt.date().isoformat()}_{dt.strftime('%H%M')}"

def _snapshot_filename(stamp: str) -> str:
    # stamp = 'YYYY-MM-DD_HHMM'
    return f"{stamp}.json"

def _snapshot_path(stamp: str) -> str:
    return os.path.join(SNAPSHOT_DIR, _snapshot_filename(stamp))

def _list_snapshot_files() -> list[str]:
    if not os.path.isdir(SNAPSHOT_DIR):
        return []
    return sorted([f for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json")])

def _list_snapshots(limit: int = 20) -> list[str]:
    files = _list_snapshot_files()
    return files[-limit:] if limit and len(files) > limit else files

def _latest_snapshot_for_date(date_str: str) -> Optional[str]:
    """Επιστρέφει filename του πιο πρόσφατου snapshot για την ημέρα (YYYY-MM-DD), ή None."""
    files = _list_snapshot_files()
    candidates = [f for f in files if f.startswith(date_str + "_")]
    return candidates[-1] if candidates else None

def _parse_snapshot_selector(selector: Optional[str]) -> Optional[str]:
    """
    Μετατρέπει επιλογή χρήστη -> filename:
    - None: πάρε το πιο πρόσφατο οποιασδήποτε μέρας
    - 'YYYY-MM-DD': πάρε το πιο πρόσφατο εκείνης της μέρας
    - 'YYYY-MM-DD_HHMM': πάρε αυτό ακριβώς
    """
    files = _list_snapshot_files()
    if not files:
        return None

    if not selector:
        return files[-1]  # latest

    selector = selector.strip()
    # ακριβές: YYYY-MM-DD_HHMM (16 chars, '_' at pos 10)
    if len(selector) == 16 and selector[10] == "_":
        candidate = _snapshot_filename(selector)
        return candidate if candidate in files else None

    # μόνο ημερομηνία
    if len(selector) == 10:
        latest_for_day = _latest_snapshot_for_date(selector)
        return latest_for_day

    return None

def _load_snapshot(selector: Optional[str] = None) -> Optional[dict]:
    """
    Διαβάζει snapshot με βάση selector (δες _parse_snapshot_selector).
    Επιστρέφει dict ή None.
    """
    fname = _parse_snapshot_selector(selector)
    if not fname:
        return None
    path = os.path.join(SNAPSHOT_DIR, fname)
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
            data["_filename"] = fname
            return data
    except Exception:
        logging.exception("Failed to read snapshot: %s", path)
        return None

def _save_snapshot(mapping: dict, totals_value: Decimal, stamp: Optional[str] = None) -> str:
    _ensure_dir(SNAPSHOT_DIR)
    st = stamp or _now_stamp()
    payload = {
        "stamp": st,  # YYYY-MM-DD_HHMM
        "date": st[:10],
        "assets": {k: {
            "amount": str(v.get("amount", 0)),
            "price_usd": str(v.get("price_usd", 0)),
            "value_usd": str(v.get("value_usd", 0)),
            "address": v.get("address"),
        } for k, v in mapping.items()},
        "totals": {"value_usd": str(totals_value)},
        "tz": TZ,
        "saved_at": _now_local().isoformat(),
    }
    path = _snapshot_path(st)
    try:
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
    except Exception:
        logging.exception("Failed to write snapshot")
    return st  # επιστρέφουμε το πλήρες stamp (YYYY-MM-DD_HHMM)

def _totals_value_from_assets(assets: List[Dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for a in assets:
        v = _to_dec(_asset_as_dict(a).get("value_usd", 0)) or Decimal("0")
        total += v
    return total

def _compare_to_snapshot(curr_total: Decimal, snap: dict) -> tuple[Decimal, Decimal, str]:
    """επιστρέφει (delta_usd, delta_pct, label) όπου label=stamp ή date."""
    try:
        snap_total = _to_dec((snap.get("totals") or {}).get("value_usd", 0)) or Decimal("0")
        label = str(snap.get("stamp") or snap.get("date") or "?")
    except Exception:
        snap_total, label = Decimal("0"), "?"
    delta = curr_total - snap_total
    pct = (delta / snap_total * Decimal("100")) if snap_total > 0 else Decimal("0")
    return delta, pct, label

# --------------------------------------------------
# Pretty formatting (compact style)
# --------------------------------------------------
def _fmt_money(x: Decimal) -> str:
    # 2 δεκαδικά και χιλιάδες με κόμμα (όπως στο δείγμα)
    q = Decimal("0.01")
    s = f"{x.quantize(q):,}"
    return s

def _fmt_price(x: Decimal) -> str:
    # αν μεγάλο, 2 δεκαδικά, αλλιώς 6…8
    try:
        if x >= Decimal("1000"):
            return f"{x:,.2f}"
        if x >= Decimal("1"):
            return f"{x:.2f}"
        elif x >= Decimal("0.01"):
            return f"{x:.6f}"
        else:
            return f"{x:.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(x)

def _fmt_qty(x: Decimal) -> str:
    # 4 δεκαδικά, με κόμματα για χιλιάδες
    try:
        return f"{x:,.4f}"
    except Exception:
        return str(x)

def _format_compact_holdings(assets: List[Dict[str, Any]], hidden_count: int) -> tuple[str, Decimal]:
    # Γραμμές τύπου:
    #  - ADA          213.5340 × $0.673332 = $143.78
    lines = ["Holdings snapshot:"]
    total = Decimal("0")
    for a in assets:
        d = _asset_as_dict(a)
        sym = str(d.get("symbol","?")).upper()
        qty = _to_dec(d.get("amount", 0)) or Decimal("0")
        px  = _to_dec(d.get("price_usd", 0)) or Decimal("0")
        val = _to_dec(d.get("value_usd", 0)) or (qty * px)
        total += val
        lines.append(
            f" - {sym:<12} {_fmt_qty(qty):>12} × ${_fmt_price(px):<12} = ${_fmt_money(val)}"
        )
    lines.append(f"\nTotal ≈ ${_fmt_money(total)}")
    if hidden_count:
        lines.append(f"\n(…και άλλα {hidden_count} κρυμμένα: spam/zero-price/dust)")
    # Quantities snapshot (runtime)
    lines.append("\nQuantities snapshot (runtime):")
    for a in assets:
        d = _asset_as_dict(a)
        sym = str(d.get("symbol","?")).upper()
        qty = _to_dec(d.get("amount", 0)) or Decimal("0")
        lines.append(f"  – {sym}: {qty}")
    return "\n".join(lines), total

# --------------------------------------------------
# Command Handlers
# --------------------------------------------------
def _handle_start() -> str:
    return (
        "👋 Γεια σου! Είμαι το Cronos DeFi Sentinel.\n\n"
        "Διαθέσιμες εντολές:\n"
        "• /holdings — snapshot χαρτοφυλακίου (compact + φίλτρα & ταξινόμηση, με PnL αν υπάρχει snapshot)\n"
        "• /scan — ωμή λίστα tokens από ανακάλυψη (χωρίς φίλτρα)\n"
        "• /rescan — πλήρης επανεύρεση & εμφάνιση (με φίλτρα)\n"
        "• /snapshot — αποθήκευση snapshot με timestamp (π.χ. 2025-10-11_0930)\n"
        "• /snapshots — λίστα διαθέσιμων snapshots\n"
        "• /pnl — δείξε PnL vs τελευταίο snapshot\n"
        "• /pnl 2025-10-11 — PnL vs πιο πρόσφατο της ημέρας\n"
        "• /pnl 2025-10-11_0930 — PnL vs συγκεκριμένο snapshot\n"
        "• /trades [SYM] — σημερινές συναλλαγές (τοπική TZ)\n"
        "• /pnl today [SYM] — σημερινό realized PnL (FIFO)\n"
        "• /help — βοήθεια"
    )

def _handle_help() -> str:
    return (
        "ℹ️ Βοήθεια\n"
        "• /holdings — compact MTM (φιλτραρισμένο) + PnL αν υπάρχει snapshot\n"
        "• /scan — ωμή λίστα tokens που βρέθηκαν (amount + address)\n"
        "• /rescan — ξανά σκανάρισμα & παρουσίαση σαν το /holdings\n"
        "• /snapshot — αποθήκευση snapshot με timestamp\n"
        "• /snapshots — λίστα διαθέσιμων snapshots\n"
        "• /pnl [ημέρα ή stamp] — PnL vs snapshot (π.χ. /pnl 2025-10-11 ή /pnl 2025-10-11_0930)\n"
        "• /trades [SYM] — σημερινές συναλλαγές (τοπική TZ)\n"
        "• /pnl today [SYM] — realized PnL μόνο για ΣΗΜΕΡΑ (FIFO ανά σύμβολο)\n"
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

    enriched = _enrich_with_prices(toks)
    cleaned, hidden_count = _filter_and_sort_assets(enriched)

    if not cleaned:
        return "🔁 Rescan ολοκληρώθηκε — δεν υπάρχει κάτι αξιοσημείωτο να εμφανιστεί (όλα φιλτραρίστηκαν ως spam/zero/dust)."

    # compact output όπως ζήτησες
    body, _ = _format_compact_holdings(cleaned, hidden_count)
    return "🔁 Rescan (filtered):\n" + body

def _handle_holdings(wallet_address: str) -> str:
    try:
        snap = get_wallet_snapshot(wallet_address)
        snap = augment_with_discovered_tokens(snap, wallet_address=wallet_address)
        snap = _normalize_snapshot_for_formatter(snap)

        assets = snap.get("assets") or []
        assets = [_asset_as_dict(a) for a in assets]

        cleaned, hidden_count = _filter_and_sort_assets(assets)
        body, total_now = _format_compact_holdings(cleaned, hidden_count)

        # PnL vs τελευταίο snapshot (αν υπάρχει)
        last_snap = _load_snapshot()  # πιο πρόσφατο διαθέσιμο
        if last_snap:
            delta, pct, label = _compare_to_snapshot(total_now, last_snap)
            sign = "+" if delta >= 0 else ""
            body += f"\n\nUnrealized PnL vs snapshot {label}: ${_fmt_money(delta)} ({sign}{pct:.2f}%)"

        return body

    except Exception:
        logging.exception("Failed to build /holdings")
        return "⚠️ Σφάλμα κατά τη δημιουργία των holdings."

def _handle_snapshot(wallet_address: str) -> str:
    """Αποθήκευση snapshot με timestamp (YYYY-MM-DD_HHMM)."""
    try:
        snap = get_wallet_snapshot(wallet_address)
        snap = augment_with_discovered_tokens(snap, wallet_address=wallet_address)
        snap = _normalize_snapshot_for_formatter(snap)
        assets = [_asset_as_dict(a) for a in (snap.get("assets") or [])]

        cleaned, _ = _filter_and_sort_assets(assets)
        mapping = _assets_list_to_mapping(cleaned)
        total_now = _totals_value_from_assets(cleaned)

        stamp = _save_snapshot(mapping, total_now)  # π.χ. '2025-10-11_1210'
        return f"💾 Snapshot saved: {stamp}. Total ≈ ${_fmt_money(total_now)}"
    except Exception:
        logging.exception("Failed to save snapshot")
        return "⚠️ Σφάλμα κατά την αποθήκευση snapshot."

def _handle_snapshots() -> str:
    files = _list_snapshots(limit=30)
    if not files:
        return "ℹ️ Δεν υπάρχουν αποθηκευμένα snapshots."
    lines = ["🗂 Διαθέσιμα snapshots (νεότερα στο τέλος):"]
    for f in files:
        stamp = f.replace(".json", "")
        lines.append(f"• {stamp}")
    return "\n".join(lines)

def _handle_pnl(wallet_address: str, arg: Optional[str] = None) -> str:
    """
    /pnl
    /pnl 2025-10-10
    /pnl 2025-10-10_0930
    """
    try:
        snap = get_wallet_snapshot(wallet_address)
        snap = augment_with_discovered_tokens(snap, wallet_address=wallet_address)
        snap = _normalize_snapshot_for_formatter(snap)
        assets = [_asset_as_dict(a) for a in (snap.get("assets") or [])]
        cleaned, _ = _filter_and_sort_assets(assets)
        total_now = _totals_value_from_assets(cleaned)

        base = _load_snapshot(arg)  # None => latest, 'YYYY-MM-DD' => latest of day, 'YYYY-MM-DD_HHMM' => exact
        if not base:
            if arg:
                return f"ℹ️ Δεν βρέθηκε snapshot για «{arg}». Δες /snapshots."
            return "ℹ️ Δεν βρέθηκε αποθηκευμένο snapshot. Στείλε /snapshot πρώτα."

        delta, pct, label = _compare_to_snapshot(total_now, base)
        sign = "+" if delta >= 0 else ""
        return f"📈 PnL vs snapshot {label}: ${_fmt_money(delta)} ({sign}{pct:.2f}%) — Now ≈ ${_fmt_money(total_now)}"
    except Exception:
        logging.exception("Failed to compute PnL")
        return "⚠️ Σφάλμα κατά τον υπολογισμό PnL."

# --- ΝΕΑ: Intraday commands (Phase 2) ------------------------------
def _handle_trades(symbol: Optional[str] = None) -> str:
    """
    /trades         -> σημερινές συναλλαγές
    /trades CRO     -> φίλτρο ανά σύμβολο
    """
    try:
        syms = [symbol.upper()] if symbol else None
        trades = todays_trades(syms)
        return format_trades_table(trades)
    except Exception:
        logging.exception("Failed to build /trades")
        return "⚠️ Σφάλμα κατά τη δημιουργία λίστας σημερινών συναλλαγών."

def _handle_pnl_today(symbol: Optional[str] = None) -> str:
    """
    /pnl today         -> realized PnL για ΣΗΜΕΡΑ (FIFO ανά σύμβολο)
    /pnl today CRO     -> φιλτραρισμένη εμφάνιση για 1 σύμβολο
    """
    try:
        summary = realized_pnl_today()
        text = format_pnl_today(summary)
        if symbol:
            sym = symbol.upper().strip()
            # Ελαφρύ filter στο ήδη formatted text (headers/totals + γραμμή sym)
            lines = []
            for line in text.splitlines():
                if line.startswith("- "):
                    if line[2:].upper().startswith(sym):
                        lines.append(line)
                else:
                    lines.append(line)
            if len(lines) > 1:
                return "\n".join(lines)
        return text
    except Exception:
        logging.exception("Failed to compute realized PnL (today)")
        return "⚠️ Σφάλμα κατά τον υπολογισμό του realized PnL για σήμερα."

# --------------------------------------------------
# Command dispatcher
# --------------------------------------------------
def _dispatch_command(text: str) -> str:
    if not text:
        return ""
    parts = text.strip().split()
    cmd = parts[0].lower()

    # Phase 2: νέα commands
    if cmd == "/trades":
        sym = parts[1] if len(parts) > 1 else None
        return _handle_trades(sym)

    if cmd == "/pnl" and len(parts) > 1 and parts[1].lower() == "today":
        sym = parts[2] if len(parts) > 2 else None
        return _handle_pnl_today(sym)

    # Existing set
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
    if cmd == "/snapshot":
        return _handle_snapshot(WALLET_ADDRESS)
    if cmd == "/snapshots":
        return _handle_snapshots()
    if cmd == "/pnl":
        arg = parts[1] if len(parts) > 1 else None
        return _handle_pnl(WALLET_ADDRESS, arg)
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
    except Exception:
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
