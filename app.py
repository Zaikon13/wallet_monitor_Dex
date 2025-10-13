# app.py
# FastAPI entrypoint για το Cronos DeFi Sentinel bot (Telegram webhook + commands)
from __future__ import annotations

import os
import re
import io
import csv
import json
import math
import time
import asyncio
import logging
import requests
from typing import Optional, List, Dict, Any, Tuple
from collections import OrderedDict, deque
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, getcontext

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import api as tg_api

# core modules (RPC-based)
from core.holdings import get_wallet_snapshot
from core.augment import augment_with_discovered_tokens
from core.discovery import discover_tokens_for_wallet
from core.pricing import get_spot_usd

getcontext().prec = 36

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
LEDGER_CSV = os.getenv("LEDGER_CSV", "./data/ledger.csv")  # default local path
HOLDINGS_BACKEND = (os.getenv("HOLDINGS_BACKEND", "auto") or "auto").lower()

# Explorer (etherscan-like) fallback
EXPLORER_BASE = (os.getenv("CRONOS_EXPLORER_API_BASE", "https://cronos.org/explorer/api").rstrip("/"))
EXPLORER_KEY = os.getenv("CRONOS_EXPLORER_API_KEY", "").strip()

# Optional live monitor task (realtime/monitor.py)
MONITOR_ENABLE = (os.getenv("MONITOR_ENABLE", "0").strip().lower() in ("1","true","yes","on"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI(title="Cronos DeFi Sentinel", version="2.2")

# --------------------------------------------------
# Telegram helpers (safe long split)
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
# Decimal & snapshot helpers
# --------------------------------------------------
def _to_dec(x):
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")

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
    nassets = []
    for x in assets:
        d = _asset_as_dict(x)
        for k in ("amount","price_usd","value_usd"):
            if k in d:
                d[k] = _to_dec(d.get(k, 0))
        nassets.append(d)
    out["assets"] = nassets
    return out

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
# Formatting
# --------------------------------------------------
def _fmt_money(x: Decimal) -> str:
    q = Decimal("0.01")
    try:
        return f"{x.quantize(q):,}"
    except Exception:
        return str(x)

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
        lines.append(f" - {sym:<12} {_fmt_qty(qty):>12} × ${_fmt_price(px):<12} = ${_fmt_money(val)}")
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
# ENV filters for holdings
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

    visible, hidden = [], 0
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
# Explorer fallback (tx + balances)
# --------------------------------------------------
def _explorer_call(params: Dict[str, Any]) -> list | dict | str | None:
    url = f"{EXPLORER_BASE}/"
    p = dict(params)
    if EXPLORER_KEY:
        p["apikey"] = EXPLORER_KEY
    try:
        r = requests.get(url, params=p, timeout=20)
        r.raise_for_status()
        j = r.json()
        return (j or {}).get("result")
    except Exception as e:
        logger.warning(f"Explorer call failed ({params.get('action')}): {e}")
        return None

def _explorer_tokentx(wallet: str) -> list[dict]:
    res = _explorer_call({"module": "account", "action": "tokentx", "address": wallet, "sort":"asc"})
    return res or []

def _explorer_txlist(wallet: str) -> list[dict]:
    res = _explorer_call({"module":"account","action":"txlist","address":wallet,"sort":"asc"})
    return res or []

def _explorer_balance_native(wallet: str) -> Decimal:
    rows = _explorer_call({"module":"account","action":"balance","address":wallet})
    val = None
    if isinstance(rows, dict):
        val = rows.get("result")
    elif isinstance(rows, str):
        val = rows
    try:
        return Decimal(str(val)) / Decimal(10**18)
    except Exception:
        return Decimal("0")

def _explorer_holdings(wallet: str) -> List[Dict[str, Any]]:
    txs = _explorer_tokentx(wallet)
    agg: Dict[Tuple[str,str,int], Decimal] = OrderedDict()
    wl = wallet.lower()
    for r in txs:
        try:
            sym = (r.get("tokenSymbol") or "").upper() or "TOKEN"
            dec = int(r.get("tokenDecimal") or 18)
            qty = _to_dec(r.get("value")) / (Decimal(10) ** dec)
            frm = (r.get("from") or "").lower()
            to  = (r.get("to") or "").lower()
            side = 1 if to == wl else (-1 if frm == wl else 0)
            if side == 0:
                continue
            key = ( (r.get("contractAddress") or "").lower(), sym, dec )
            agg[key] = agg.get(key, Decimal("0")) + side * qty
        except Exception:
            continue

    assets: List[Dict[str, Any]] = []
    cro_bal = _explorer_balance_native(wallet)
    if cro_bal > 0:
        px = _to_dec(get_spot_usd("CRO", token_address=None)) or Decimal("0")
        assets.append({"symbol":"CRO","amount":cro_bal,"price_usd":px,"value_usd":cro_bal*px})

    for (contract, sym, _decimals), qty in agg.items():
        if qty <= 0:
            continue
        px = _to_dec(get_spot_usd(sym, token_address=contract)) or Decimal("0")
        assets.append({
            "symbol": sym,
            "amount": qty,
            "price_usd": px,
            "value_usd": qty * px,
            "address": contract,
        })
    return assets

def _holdings_auto(wallet: str) -> Tuple[List[Dict[str, Any]], str]:
    if HOLDINGS_BACKEND in ("rpc","auto"):
        try:
            snap = get_wallet_snapshot(wallet)
            snap = augment_with_discovered_tokens(snap, wallet_address=wallet)
            snap = _normalize_snapshot_for_formatter(snap)
            assets = [_asset_as_dict(a) for a in (snap.get("assets") or [])]
            for a in assets:
                if _to_dec(a.get("price_usd",0)) <= 0:
                    a["price_usd"] = _to_dec(get_spot_usd(str(a.get("symbol","")), token_address=a.get("address")))
                amt = _to_dec(a.get("amount",0))
                px  = _to_dec(a.get("price_usd",0))
                a["value_usd"] = _to_dec(a.get("value_usd", amt*px)) or (amt*px)
            return assets, "rpc"
        except Exception:
            logger.warning("RPC holdings failed, switching to Explorer")
    return _explorer_holdings(wallet), "explorer"

# --------------------------------------------------
# Ledger (for /trades and /pnl today)
# --------------------------------------------------
def _ensure_ledger():
    base = os.path.dirname(LEDGER_CSV)
    if base:
        os.makedirs(base, exist_ok=True)
    if not os.path.exists(LEDGER_CSV):
        with open(LEDGER_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["ts","symbol","qty","side","price_usd","tx"])
            w.writeheader()

def _read_ledger_rows(start_dt: datetime, end_dt: datetime, symbol: Optional[str] = None) -> List[dict]:
    _ensure_ledger()
    out = []
    try:
        with open(LEDGER_CSV, "r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    ts = datetime.fromisoformat(row["ts"])
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=ZoneInfo(TZ))
                except Exception:
                    try:
                        ts = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(TZ))
                    except Exception:
                        continue
                if not (start_dt <= ts <= end_dt):
                    continue
                sym = (row.get("symbol") or "").upper()
                if symbol and sym != symbol.upper():
                    continue
                qty = _to_dec(row.get("qty", "0"))
                side = (row.get("side") or "").upper()
                px = _to_dec(row.get("price_usd", "0"))
                tx = row.get("tx") or ""
                out.append({"ts": ts, "symbol": sym, "qty": qty, "side": side, "price_usd": px, "tx": tx})
    except FileNotFoundError:
        out = []
    except Exception:
        logging.exception("Failed reading ledger")
    out.sort(key=lambda x: x["ts"])
    return out

def _today_range() -> Tuple[datetime, datetime]:
    now = _now_local()
    start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=ZoneInfo(TZ))
    end   = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=ZoneInfo(TZ))
    return start, end

def _fifo_realized_pnl(rows: List[dict]) -> Tuple[Decimal, Dict[str, Decimal]]:
    per_sym_lots: Dict[str, deque] = {}
    realized_total = Decimal("0")
    realized_by_sym: Dict[str, Decimal] = {}

    for r in rows:
        sym = r["symbol"]
        qty = r["qty"]
        side = r["side"]
        px = r["price_usd"]
        if sym not in per_sym_lots:
            per_sym_lots[sym] = deque()

        if side == "IN":
            per_sym_lots[sym].append({"qty": qty, "px": px})
        elif side == "OUT":
            to_sell = qty if qty > 0 else -qty
            remain = to_sell
            realized = Decimal("0")
            while remain > 0 and per_sym_lots[sym]:
                lot = per_sym_lots[sym][0]
                use = min(remain, lot["qty"])
                realized += (px - lot["px"]) * use
                lot["qty"] -= use
                remain -= use
                if lot["qty"] <= 0:
                    per_sym_lots[sym].popleft()
            realized_total += realized
            realized_by_sym[sym] = realized_by_sym.get(sym, Decimal("0")) + realized

    return realized_total, realized_by_sym

def _format_trades_output(rows: List[dict], title: str) -> str:
    if not rows:
        return "Σήμερα δεν βρέθηκαν συναλλαγές."
    lines = [title, "Transactions:"]
    net_flow = Decimal("0")
    for r in rows:
        flow = r["qty"] * r["price_usd"] * (Decimal("1") if r["side"]=="IN" else Decimal("-1"))
        net_flow += flow
        lines.append(f"• {r['ts'].strftime('%H:%M:%S')} — {r['side']} {r['symbol']} {_fmt_qty(r['qty'])}  @ ${_fmt_price(r['price_usd'])}  (${_fmt_money(flow)})")
    lines.append(f"\nNet USD flow today: ${_fmt_money(net_flow)}")
    return "\n".join(lines)

def _explorer_backfill_today_to_ledger(wallet: str) -> int:
    _ensure_ledger()
    start, end = _today_range()
    wl = wallet.lower()
    wrote = 0

    # 1) CRC20 transfers
    tok = _explorer_tokentx(wallet)
    with open(LEDGER_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ts","symbol","qty","side","price_usd","tx"])
        for r in tok:
            try:
                ts = datetime.fromtimestamp(int(r.get("timeStamp", "0")), tz=ZoneInfo(TZ))
                if not (start <= ts <= end):
                    continue
                sym = (r.get("tokenSymbol") or "").upper() or "TOKEN"
                dec = int(r.get("tokenDecimal") or 18)
                qty = _to_dec(r.get("value")) / (Decimal(10) ** dec)
                frm = (r.get("from") or "").lower()
                to  = (r.get("to") or "").lower()
                if frm == wl and to != wl:
                    side = "OUT"
                    signed_qty = qty
                elif to == wl and frm != wl:
                    side = "IN"
                    signed_qty = qty
                else:
                    continue
                px = _to_dec(get_spot_usd(sym, token_address=(r.get("contractAddress") or "").lower()))
                w.writerow({
                    "ts": ts.isoformat(),
                    "symbol": sym,
                    "qty": str(signed_qty),
                    "side": side,
                    "price_usd": str(px),
                    "tx": r.get("hash") or "",
                })
                wrote += 1
            except Exception:
                continue

    # 2) Native CRO transfers
    txs = _explorer_txlist(wallet)
    with open(LEDGER_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ts","symbol","qty","side","price_usd","tx"])
        for r in txs:
            try:
                ts = datetime.fromtimestamp(int(r.get("timeStamp", "0")), tz=ZoneInfo(TZ))
                if not (start <= ts <= end):
                    continue
                value_wei = _to_dec(r.get("value"))
                if value_wei <= 0:
                    continue
                qty = value_wei / Decimal(10**18)
                frm = (r.get("from") or "").lower()
                to  = (r.get("to") or "").lower()
                if frm == wl and to != wl:
                    side = "OUT"
                elif to == wl and frm != wl:
                    side = "IN"
                else:
                    continue
                px = _to_dec(get_spot_usd("CRO", token_address=None))
                w.writerow({
                    "ts": ts.isoformat(),
                    "symbol": "CRO",
                    "qty": str(qty),
                    "side": side,
                    "price_usd": str(px),
                    "tx": r.get("hash") or "",
                })
                wrote += 1
            except Exception:
                continue

    return wrote

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
    return _handle_start().replace("👋 Γεια σου! Είμαι το Cronos DeFi Sentinel.\n\n", "ℹ️ Βοήθεια\n")

def _handle_scan(wallet_address: str) -> str:
    try:
        toks = discover_tokens_for_wallet(wallet_address)
    except Exception:
        toks = []
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
    try:
        toks = discover_tokens_for_wallet(wallet_address)
    except Exception:
        toks = []
    if not toks:
        # fallback: holdings auto (rpc or explorer) για να δείξουμε κάτι
        assets, src = _holdings_auto(wallet_address)
        cleaned, hidden_count = _filter_and_sort_assets(assets)
        if not cleaned:
            return "🔁 Rescan ολοκληρώθηκε — δεν υπάρχει κάτι αξιοσημείωτο (ενδέχεται rate-limit)."
        body, _ = _format_compact_holdings(cleaned, hidden_count)
        return "🔁 Rescan (fallback from holdings):\n" + body

    enriched = _enrich_with_prices(toks)
    cleaned, hidden_count = _filter_and_sort_assets(enriched)
    if not cleaned:
        return "🔁 Rescan ολοκληρώθηκε — δεν υπάρχει κάτι αξιοσημείωτο να εμφανιστεί (όλα φιλτραρίστηκαν ως spam/zero/dust)."
    body, _ = _format_compact_holdings(cleaned, hidden_count)
    return "🔁 Rescan (filtered):\n" + body

def _handle_holdings(wallet_address: str) -> str:
    try:
        assets, src = _holdings_auto(wallet_address)
        cleaned, hidden_count = _filter_and_sort_assets(assets)
        if not cleaned:
            return "⚠️ Δεν μπόρεσα να εμφανίσω holdings (πιθανό rate-limit στο RPC/Explorer). Δοκίμασε ξανά."
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
    try:
        assets, _ = _holdings_auto(wallet_address)
        cleaned, _ = _filter_and_sort_assets(assets)
        mapping = _assets_list_to_mapping(cleaned)
        total_now = _totals_value_from_assets(cleaned)
        stamp = _save_snapshot(mapping, total_now)
        return f"💾 Snapshot saved: {stamp}. Total ≈ ${_fmt_money(total_now)}"
    except Exception:
        logging.exception("Failed to save snapshot")
        return "⚠️ Σφάλμα κατά την αποθήκευση snapshot."

def _handle_snapshots() -> str:
    files = _list_snapshots(limit=50)
    if not files:
        return "ℹ️ Δεν υπάρχουν αποθηκευμένα snapshots."
    lines = ["🗂 Διαθέσιμα snapshots (νεότερα στο τέλος):"]
    for f in files:
        stamp = f.replace(".json", "")
        lines.append(f"• {stamp}")
    return "\n".join(lines)

def _handle_pnl(wallet_address: str, arg: Optional[str] = None) -> str:
    try:
        assets, _ = _holdings_auto(wallet_address)
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

def _handle_trades(symbol: Optional[str] = None) -> str:
    start, end = _today_range()
    rows = _read_ledger_rows(start, end, symbol)
    if not rows:
        # κάνε backfill από explorer για ΣΗΜΕΡΑ και ξαναδοκίμασε
        try:
            wrote = _explorer_backfill_today_to_ledger(WALLET_ADDRESS)
            if wrote > 0:
                rows = _read_ledger_rows(start, end, symbol)
        except Exception:
            pass
    title = f"🟡 Intraday Update\n📒 Daily Report ({_today_str()})"
    return _format_trades_output(rows, title)

def _handle_pnl_today(symbol: Optional[str] = None) -> str:
    start, end = _today_range()
    rows = _read_ledger_rows(start, end, symbol)
    if not rows:
        # backfill σημερινών κινήσεων αν δεν υπάρχουν
        try:
            wrote = _explorer_backfill_today_to_ledger(WALLET_ADDRESS)
            if wrote > 0:
                rows = _read_ledger_rows(start, end, symbol)
        except Exception:
            pass

    realized_total, realized_by_sym = _fifo_realized_pnl(rows)
    lines = [f"📊 Realized PnL (today {_today_str()}, FIFO):"]
    if symbol:
        sym = symbol.upper()
        val = realized_by_sym.get(sym, Decimal("0"))
        sign = "+" if val >= 0 else ""
        lines.append(f"• {sym}: {sign}${_fmt_money(val)}")
    else:
        for sym, val in sorted(realized_by_sym.items()):
            sign = "+" if val >= 0 else ""
            lines.append(f"• {sym}: {sign}${_fmt_money(val)}")
        sign = "+" if realized_total >= 0 else ""
        lines.append(f"\nTotal realized today: {sign}${_fmt_money(realized_total)}")
    return "\n".join(lines)

# --------------------------------------------------
# Command dispatcher
# --------------------------------------------------
def _dispatch_command(text: str) -> str:
    if not text:
        return ""
    parts = text.strip().split()
    cmd = parts[0].lower()

    # /pnl today [SYM]
    if cmd == "/pnl" and len(parts) >= 2 and parts[1].lower() == "today":
        sym = parts[2] if len(parts) >= 3 else None
        return _handle_pnl_today(sym)

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
    if cmd == "/trades":
        sym = parts[1] if len(parts) > 1 else None
        return _handle_trades(sym)
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
# Optional: live monitor supervisor (won't crash API)
# --------------------------------------------------
async def _supervisor():
    if not MONITOR_ENABLE:
        return
    try:
        from realtime.monitor import monitor_wallet
    except Exception as e:
        logger.error("Monitor disabled (import error): %s", e)
        return

    async def _sender(msg: str):
        try:
            send_message(msg, CHAT_ID)
        except Exception:
            pass

    while True:
        try:
            await monitor_wallet(_sender, logger=logging.getLogger("realtime"))
        except Exception as e:
            logging.error("monitor_wallet crashed: %s", e)
        await asyncio.sleep(3.0)  # small backoff, keep API alive

# --------------------------------------------------
# Startup
# --------------------------------------------------
@app.on_event("startup")
async def on_startup():
    logging.info("✅ Cronos DeFi Sentinel started and is online.")
    try:
        _ensure_dir(os.path.dirname(LEDGER_CSV) or ".")
        _ensure_dir(SNAPSHOT_DIR)
    except Exception:
        pass
    try:
        send_message("✅ Cronos DeFi Sentinel started and is online.", CHAT_ID)
    except Exception:
        pass
    # fire-and-forget supervisor (does nothing unless MONITOR_ENABLE=1)
    asyncio.create_task(_supervisor())
