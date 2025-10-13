# app.py
# FastAPI entrypoint για το Cronos DeFi Sentinel bot (Telegram webhook + commands + realtime monitor)
from __future__ import annotations

import os
import re
import csv
import json
import logging
from typing import Optional, List, Dict, Any
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import api as tg_api
# from telegram.formatters import format_holdings as _unused_external_formatter

from core.holdings import get_wallet_snapshot
from core.augment import augment_with_discovered_tokens
from core.discovery import discover_tokens_for_wallet
from core.pricing import get_spot_usd

# --------------------------------------------------
# Configuration
# --------------------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0")) or None
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
APP_URL = os.getenv("APP_URL")  # optional
EOD_TIME = os.getenv("EOD_TIME", "23:59")
TZ = os.getenv("TZ", "Europe/Athens")
SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", "./data/snapshots")
LEDGER_CSV = os.getenv("LEDGER_CSV", "data/ledger.csv")

# Realtime monitor toggles (supervisor ξεκινάει monitor αν υπάρχει)
MONITOR_ENABLE = os.getenv("MONITOR_ENABLE", "1").lower() in ("1","true","yes","on")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI(title="Cronos DeFi Sentinel", version="1.2")

# --------------------------------------------------
# Telegram helpers
# --------------------------------------------------
def _fallback_send_message(text: str, chat_id: Optional[int] = None):
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
    _send_long_text(text, chat_id)

# --------------------------------------------------
# Snapshot normalization helpers
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
# ENV-driven filters/sorting
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
# Snapshots + PnL helpers
# --------------------------------------------------
def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def _now_local() -> datetime:
    return datetime.now(ZoneInfo(TZ))

def _today_str() -> str:
    return _now_local().date().isoformat()

def _now_stamp() -> str:
    dt = _now_local()
    return f"{dt.date().isoformat()}_{dt.strftime('%H%M')}"

def _snapshot_filename(stamp: str) -> str:
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
    files = _list_snapshot_files()
    candidates = [f for f in files if f.startswith(date_str + "_")]
    return candidates[-1] if candidates else None

def _parse_snapshot_selector(selector: Optional[str]) -> Optional[str]:
    files = _list_snapshot_files()
    if not files:
        return None
    if not selector:
        return files[-1]
    selector = selector.strip()
    if len(selector) == 16 and selector[10] == "_":
        candidate = _snapshot_filename(selector)
        return candidate if candidate in files else None
    if len(selector) == 10:
        latest_for_day = _latest_snapshot_for_date(selector)
        return latest_for_day
    return None

def _load_snapshot(selector: Optional[str] = None) -> Optional[dict]:
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
        "stamp": st,
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
    return st

def _totals_value_from_assets(assets: List[Dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for a in assets:
        v = _to_dec(_asset_as_dict(a).get("value_usd", 0)) or Decimal("0")
        total += v
    return total

def _compare_to_snapshot(curr_total: Decimal, snap: dict) -> tuple[Decimal, Decimal, str]:
    try:
        snap_total = _to_dec((snap.get("totals") or {}).get("value_usd", 0)) or Decimal("0")
        label = str(snap.get("stamp") or snap.get("date") or "?")
    except Exception:
        snap_total, label = Decimal("0"), "?"
    delta = curr_total - snap_total
    pct = (delta / snap_total * Decimal("100")) if snap_total > 0 else Decimal("0")
    return delta, pct, label

# --------------------------------------------------
# Pretty formatting
# --------------------------------------------------
def _fmt_money(x: Decimal) -> str:
    q = Decimal("0.01")
    s = f"{x.quantize(q):,}"
    return s

def _fmt_price(x: Decimal) -> str:
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
    try:
        return f"{x:,.4f}"
    except Exception:
        return str(x)

def _format_compact_holdings(assets: List[Dict[str, Any]], hidden_count: int) -> tuple[str, Decimal]:
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
    lines.append("\nQuantities snapshot (runtime):")
    for a in assets:
        d = _asset_as_dict(a)
        sym = str(d.get("symbol","?")).upper()
        qty = _to_dec(d.get("amount", 0)) or Decimal("0")
        lines.append(f"  – {sym}: {qty}")
    return "\n".join(lines), total

# --------------------------------------------------
# Ledger readers (for /trades & /pnl today)
# --------------------------------------------------
_LOCAL_TZ = ZoneInfo(TZ)

def _read_ledger_today(sym_filter: Optional[str]=None) -> list[dict]:
    out = []
    if not os.path.exists(LEDGER_CSV):
        return out
    today = datetime.now(_LOCAL_TZ).date()
    with open(LEDGER_CSV, "r", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            ts_raw = row.get("ts") or ""
            try:
                # support "2025-10-12T12:03:00+00:00" or Z
                ts = datetime.fromisoformat(ts_raw.replace("Z","+00:00"))
            except Exception:
                continue
            ts_local = ts.astimezone(_LOCAL_TZ)
            if ts_local.date() != today:
                continue
            sym = (row.get("symbol") or "").upper()
            if sym_filter and sym != sym_filter.upper():
                continue
            try:
                qty = Decimal(str(row.get("qty") or "0"))
                price = Decimal(str(row.get("price") or "0"))
                fee = Decimal(str(row.get("fee") or "0"))
            except Exception:
                qty = Decimal("0"); price = Decimal("0"); fee = Decimal("0")
            out.append({
                "ts": ts_local,
                "symbol": sym,
                "side": (row.get("side") or "").upper(),  # BUY/SELL
                "qty": qty,
                "price": price,
                "fee": fee,
                "tx": row.get("tx") or "",
            })
    out.sort(key=lambda x: x["ts"])
    return out

def _fifo_realized_pnl_today(rows: list[dict]) -> tuple[Decimal, dict]:
    realized_total = Decimal("0")
    realized_by_sym: dict[str, Decimal] = {}
    inv: dict[str, list[tuple[Decimal, Decimal]]] = {}  # sym -> [(qty, price)]
    for r in rows:
        s = r["symbol"]
        side = r["side"]
        qty = r["qty"]
        px  = r["price"]
        if px <= 0 or qty <= 0:
            continue
        if s not in inv:
            inv[s] = []
        if side == "BUY":
            inv[s].append((qty, px))
        elif side == "SELL":
            remain = qty
            pnl = Decimal("0")
            while remain > 0 and inv[s]:
                lot_qty, lot_px = inv[s][0]
                take = min(remain, lot_qty)
                pnl += (px - lot_px) * take
                lot_qty -= take
                remain -= take
                if lot_qty <= 0:
                    inv[s].pop(0)
                else:
                    inv[s][0] = (lot_qty, lot_px)
            realized_by_sym[s] = realized_by_sym.get(s, Decimal("0")) + pnl
            realized_total += pnl
    return realized_total, realized_by_sym

# --------------------------------------------------
# Command Handlers
# --------------------------------------------------
def _handle_start() -> str:
    return (
        "👋 Γεια σου! Είμαι το Cronos DeFi Sentinel.\n\n"
        "Διαθέσιμες εντολές:\n"
        "• /holdings — snapshot χαρτοφυλακίου (compact + φίλτρα, με PnL vs τελευταίο)\n"
        "• /scan — ωμή λίστα tokens από ανακάλυψη (χωρίς φίλτρα)\n"
        "• /rescan — πλήρης επανεύρεση & εμφάνιση (με φίλτρα)\n"
        "• /snapshot — αποθήκευση snapshot με timestamp (π.χ. 2025-10-11_0930)\n"
        "• /snapshots — λίστα διαθέσιμων snapshots\n"
        "• /pnl — PnL vs snapshot [ημέρα ή stamp]\n"
        "• /trades [SYM] — σημερινές συναλλαγές (τοπική TZ)\n"
        "• /pnl today [SYM] — realized PnL (σήμερα, FIFO)\n"
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
        "• /trades [SYM] — σημερινές συναλλαγές\n"
        "• /pnl today [SYM] — realized PnL για σήμερα (FIFO)\n"
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
        last_snap = _load_snapshot()
        if last_snap:
            delta, pct, label = _compare_to_snapshot(total_now, last_snap)
            sign = "+" if delta >= 0 else ""
            body += f"\n\nUnrealized PnL vs snapshot {label}: ${_fmt_money(delta)} ({sign}{pct:.2f}%)"
        return body
    except Exception:
        logging.exception("Failed to build /holdings")
        return "⚠️ Σφάλμα κατά τη δημιουργία των holdings."

def _handle_snapshot(wallet_address: str) -> str:
    try:
        snap = get_wallet_snapshot(wallet_address)
        snap = augment_with_discovered_tokens(snap, wallet_address=wallet_address)
        snap = _normalize_snapshot_for_formatter(snap)
        assets = [_asset_as_dict(a) for a in (snap.get("assets") or [])]
        cleaned, _ = _filter_and_sort_assets(assets)
        mapping = _assets_list_to_mapping(cleaned)
        total_now = _totals_value_from_assets(cleaned)
        stamp = _save_snapshot(mapping, total_now)
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
    try:
        snap = get_wallet_snapshot(wallet_address)
        snap = augment_with_discovered_tokens(snap, wallet_address=wallet_address)
        snap = _normalize_snapshot_for_formatter(snap)
        assets = [_asset_as_dict(a) for a in (snap.get("assets") or [])]
        cleaned, _ = _filter_and_sort_assets(assets)
        total_now = _totals_value_from_assets(cleaned)

        base = _load_snapshot(arg)
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

# -------- NEW: /trades (today) --------
def _handle_trades_cmd(sym: Optional[str]=None) -> str:
    rows = _read_ledger_today(sym_filter=sym)
    if not rows:
        return "Σήμερα δεν βρέθηκαν συναλλαγές."
    lines = ["🟡 Intraday Trades (today):"]
    for r in rows:
        t = r["ts"].strftime("%H:%M:%S")
        s = r["symbol"]
        side = "IN " if r["side"]=="BUY" else "OUT"
        qty = f"{r['qty']:,.6f}".rstrip("0").rstrip(".")
        px  = f"{r['price']:,.6f}".rstrip("0").rstrip(".")
        txs = r['tx']
        tx_short = f"{txs[:10]}…{txs[-8:]}" if txs else "-"
        lines.append(f"• {t} — {side} {s} {qty} @ ${px} (tx {tx_short})")
    return "\n".join(lines)

# -------- NEW: /pnl today (FIFO) --------
def _handle_pnl_today(sym: Optional[str]=None) -> str:
    rows = _read_ledger_today(sym_filter=sym)
    if not rows:
        return "Σήμερα δεν βρέθηκαν συναλλαγές."
    realized, by_sym = _fifo_realized_pnl_today(rows)
    lines = [f"📊 Realized PnL (today) — FIFO"]
    if sym:
        v = by_sym.get(sym.upper(), Decimal("0"))
        lines.append(f"• {sym.upper()}: ${_fmt_money(v)}")
    else:
        for s, v in sorted(by_sym.items()):
            lines.append(f"• {s}: ${_fmt_money(v)}")
        lines.append(f"\nTotal realized today: ${_fmt_money(realized)}")
    return "\n".join(lines)

# --------------------------------------------------
# Command dispatcher
# --------------------------------------------------
def _dispatch_command(text: str) -> str:
    if not text:
        return ""
    parts = text.strip().split()
    cmd = parts[0].lower()
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
        # support "/pnl today [SYM]" and "/pnl [date|stamp]"
        if len(parts) > 1 and parts[1].lower() == "today":
            sym = parts[2] if len(parts) > 2 else None
            return _handle_pnl_today(sym)
        arg = parts[1] if len(parts) > 1 else None
        return _handle_pnl(WALLET_ADDRESS, arg)
    if cmd == "/trades":
        sym = parts[1] if len(parts) > 1 else None
        return _handle_trades_cmd(sym)
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
# Startup: boot notice + realtime supervisor
# --------------------------------------------------
@app.on_event("startup")
async def on_startup():
    logging.info("✅ Cronos DeFi Sentinel started and is online.")
    try:
        send_message("✅ Cronos DeFi Sentinel started and is online.", CHAT_ID)
    except Exception:
        pass

    if MONITOR_ENABLE:
        # try import realtime monitor and start a supervised background task
        try:
            import asyncio
            from realtime.monitor import monitor_wallet

            async def _sender(msg: str):
                try:
                    send_message(msg, CHAT_ID)
                except Exception:
                    pass

            async def _supervisor():
                while True:
                    try:
                        await monitor_wallet(_sender, logger=logging.getLogger("realtime"))
                    except Exception as e:
                        logging.error("monitor_wallet crashed: %s", e)
                        await asyncio.sleep(3)

            loop = asyncio.get_event_loop()
            loop.create_task(_supervisor())
        except Exception as e:
            logging.error("Realtime monitor not started: %s", e)
