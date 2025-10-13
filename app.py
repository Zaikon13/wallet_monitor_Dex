# ==== START app.py (Part 1/3) ================================================
# app.py
# Cronos DeFi Sentinel — FastAPI + Telegram webhook + commands + live monitor bootstrap
from __future__ import annotations

import os
import re
import csv
import json
import time
import asyncio
import logging
import requests
from typing import Optional, List, Dict, Any, Tuple
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, getcontext

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# (στο repo σου) Telegram helper
from telegram import api as tg_api

# core (RPC-based holdings + discovery + pricing)
from core.holdings import get_wallet_snapshot          # used for primary holdings
from core.augment import augment_with_discovered_tokens
from core.discovery import discover_tokens_for_wallet
from core.pricing import get_spot_usd                  # pricing spot USD
# live monitor loop (separate module, already uploaded)
# monitor writes ./data/ledger.csv and sends alerts on swaps/in/out
import monitor as rt_monitor  # uses CRONOS_EXPLORER_API_BASE + KEY internally

getcontext().prec = 36

# ============ Config ============
BOT_TOKEN      = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID        = int(os.getenv("TELEGRAM_CHAT_ID", "0")) or None
WALLET_ADDRESS = (os.getenv("WALLET_ADDRESS") or "").strip()
TZ             = os.getenv("TZ", "Europe/Athens")

SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", "./data/snapshots")
LEDGER_CSV   = os.getenv("LEDGER_CSV", "./data/ledger.csv")

HOLDINGS_BACKEND = (os.getenv("HOLDINGS_BACKEND", "auto") or "auto").lower()

# Explorer (ΜΟΝΟ «BASE» όπως ζήτησες)
# examples:
#  - https://explorer-api.cronos.org/mainnet/api/v1
#  - https://cronos.org/explorer/api
CRONOS_EXPLORER_API_BASE = (os.getenv("CRONOS_EXPLORER_API_BASE", "https://cronos.org/explorer/api").rstrip("/"))
CRONOS_EXPLORER_API_KEY  = (os.getenv("CRONOS_EXPLORER_API_KEY", "") or "").strip()

# Live monitor toggle
MONITOR_ENABLE  = (os.getenv("MONITOR_ENABLE", "0").strip().lower() in ("1", "true", "yes", "on"))
RT_POLL_SEC     = float(os.getenv("RT_POLL_SEC", "1.2"))
RT_BACKOFF_MAX  = int(os.getenv("RT_BACKOFF_MAX_SEC", "20"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

app = FastAPI(title="Cronos DeFi Sentinel", version="3.0")

# ============ Telegram send (safe chunk) ============
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
        logging.exception(f"Telegram module send failed: {e}")

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token or not chat_id:
        logging.error("No BOT_TOKEN or chat_id for HTTP fallback.")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code >= 400:
            logging.error("Telegram HTTP send failed: %s %s", r.status_code, r.text)
    except Exception as e:
        logging.exception(f"Telegram HTTP send exception: {e}")

def _send_long_text(text: str, chat_id: Optional[int], chunk: int = 3500) -> None:
    if not text:
        return
    t = text
    while len(t) > chunk:
        cut = t.rfind("\n", 0, chunk)
        if cut == -1:
            cut = chunk
        _fallback_send_message(t[:cut], chat_id)
        t = t[cut:]
    _fallback_send_message(t, chat_id)

def send_message(text: str, chat_id: Optional[int] = None) -> None:
    _send_long_text(text, chat_id)

# ============ helpers: Decimal / FS ============
def _to_dec(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")

def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def _now() -> datetime:
    return datetime.now(ZoneInfo(TZ))

def _today_range() -> Tuple[datetime, datetime]:
    now = _now()
    start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=ZoneInfo(TZ))
    end   = datetime(now.year, now.month, now.day, 23, 59, 59, tzinfo=ZoneInfo(TZ))
    return start, end

# ============ Explorer (fallback for holdings & quick scans) ============
def _explorer_call(module: str, action: str, **params) -> Any:
    """
    Συμβατό με etherscan-like & v1 route:
    - Αν το BASE τελειώνει σε '/api' ή '/api/v1', περνάμε ?module=...&action=...
    - Διαφορετικά, προσθέτουμε '/api' (συμβατότητα).
    Backoff σε 500/429 (σύντομο & αθόρυβο).
    """
    base = CRONOS_EXPLORER_API_BASE
    url = base
    if not base.endswith("/api") and not base.endswith("/api/v1"):
        url = base + "/api"

    q = {"module": module, "action": action}
    q.update(params)
    if CRONOS_EXPLORER_API_KEY:
        q["apikey"] = CRONOS_EXPLORER_API_KEY

    backoff = 1.0
    for _ in range(6):
        try:
            r = requests.get(url, params=q, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(backoff, RT_BACKOFF_MAX))
                backoff *= 1.6
                continue
            r.raise_for_status()
            jd = r.json()
            # v1: {"status":"1","message":"OK","result":[...]} OR plain list/dict
            return jd.get("result", jd)
        except requests.exceptions.RequestException as e:
            logging.warning(f"Explorer call fail [{module}.{action}]: {e}")
            time.sleep(min(backoff, RT_BACKOFF_MAX))
            backoff *= 1.6
        except Exception as e:
            logging.warning(f"Explorer parse fail [{module}.{action}]: {e}")
            break
    return None

def _explorer_tokentx(wallet: str) -> List[dict]:
    res = _explorer_call("account", "tokentx", address=wallet, sort="asc")
    return res or []

def _explorer_txlist(wallet: str) -> List[dict]:
    res = _explorer_call("account", "txlist", address=wallet, sort="asc")
    return res or []

def _explorer_balance_native(wallet: str) -> Decimal:
    res = _explorer_call("account", "balance", address=wallet)
    val = None
    if isinstance(res, dict):
        val = res.get("result") or res.get("balance")
    elif isinstance(res, str):
        val = res
    try:
        # wei → CRO (18)
        return _to_dec(val) / Decimal(10**18)
    except Exception:
        return Decimal("0")
# ==== END app.py (Part 1/3) ==================================================
# ==== START app.py (Part 2/3) ================================================
# ============ Holdings (RPC primary, explorer fallback) ============
def _asset_line(sym: str, qty: Decimal, px: Decimal, val: Decimal) -> str:
    def _fmt_price(x: Decimal) -> str:
        try:
            if x >= Decimal("1000"): return f"{x:,.2f}"
            if x >= Decimal("1"):    return f"{x:.2f}"
            if x >= Decimal("0.01"): return f"{x:.6f}"
            return f"{x:.8f}".rstrip("0").rstrip(".")
        except Exception:
            return str(x)
    def _fmt_qty(x: Decimal) -> str:
        try:
            return f"{x:,.4f}"
        except Exception:
            return str(x)
    def _fmt_money(x: Decimal) -> str:
        try:
            return f"{x:,.2f}"
        except Exception:
            return str(x)
    return f" - {sym:<10} {_fmt_qty(qty):>12} × ${_fmt_price(px):<12} = ${_fmt_money(val)}"

def _holdings_explorer(wallet: str) -> List[Dict[str, Any]]:
    wl = (wallet or "").lower()
    txs = _explorer_tokentx(wallet)
    agg: Dict[Tuple[str, str, int], Decimal] = OrderedDict()
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
            key = ((r.get("contractAddress") or "").lower(), sym, dec)
            agg[key] = agg.get(key, Decimal("0")) + side * qty
        except Exception:
            continue

    assets: List[Dict[str, Any]] = []
    # native CRO
    cro_bal = _explorer_balance_native(wallet)
    if cro_bal > 0:
        px = _to_dec(get_spot_usd("CRO", token_address=None)) or Decimal("0")
        assets.append({"symbol": "CRO", "amount": cro_bal, "price_usd": px, "value_usd": cro_bal * px})

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
    if HOLDINGS_BACKEND in ("rpc", "auto"):
        try:
            snap = get_wallet_snapshot(wallet)                                   # primary RPC snapshot
            snap = augment_with_discovered_tokens(snap, wallet_address=wallet)   # add discovered
            assets = []
            for a in (snap.get("assets") or []):
                sym = str(a.get("symbol") or "").upper() or "TOKEN"
                amt = _to_dec(a.get("amount", 0))
                px  = _to_dec(a.get("price_usd", 0))
                if px <= 0:
                    px = _to_dec(get_spot_usd(sym, token_address=a.get("address")))
                val = amt * px if px > 0 else _to_dec(a.get("value_usd", 0))
                assets.append({
                    "symbol": sym, "amount": amt, "price_usd": px, "value_usd": val, "address": a.get("address")
                })
            return assets, "rpc"
        except Exception:
            logging.warning("RPC holdings failed, fallback to explorer")
    return _holdings_explorer(wallet), "explorer"

# ============ Snapshots ============
def _ensure_ledger():
    base = os.path.dirname(LEDGER_CSV)
    if base:
        os.makedirs(base, exist_ok=True)
    if not os.path.exists(LEDGER_CSV):
        with open(LEDGER_CSV, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["ts", "symbol", "qty", "side", "price_usd", "tx"])
            w.writeheader()

def _snap_dir():
    _ensure_dir(SNAPSHOT_DIR)

def _stamp_now() -> str:
    dt = _now()
    return f"{dt.date().isoformat()}_{dt.strftime('%H%M')}"

def _snap_path(stamp: str) -> str:
    return os.path.join(SNAPSHOT_DIR, f"{stamp}.json")

def _save_snapshot(assets: List[Dict[str, Any]], stamp: Optional[str] = None) -> str:
    _snap_dir()
    st = stamp or _stamp_now()
    total = sum((_to_dec(a.get("value_usd", 0)) for a in assets), Decimal("0"))
    payload = {
        "stamp": st,
        "date": st[:10],
        "assets": {
            (a.get("symbol") or "?"): {
                "amount": str(a.get("amount", 0)),
                "price_usd": str(a.get("price_usd", 0)),
                "value_usd": str(a.get("value_usd", 0)),
                "address": a.get("address"),
            } for a in assets
        },
        "totals": {"value_usd": str(total)},
        "tz": TZ,
        "saved_at": _now().isoformat(),
    }
    with open(_snap_path(st), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return st

def _list_snapshots(limit: int = 24) -> List[str]:
    if not os.path.isdir(SNAPSHOT_DIR):
        return []
    files = sorted([f for f in os.listdir(SNAPSHOT_DIR) if f.endswith(".json")])
    return files[-limit:] if limit else files

def _load_snapshot(selector: Optional[str]) -> Optional[dict]:
    if not selector:
        files = _list_snapshots(1)
        if not files:
            return None
        selector = files[-1]
    # selector can be 'YYYY-MM-DD' (choose latest in day) or full stamp 'YYYY-MM-DD_HHMM'
    if len(selector) == 10:
        prefix = selector + "_"
        cands = [f for f in _list_snapshots(0) if f.startswith(prefix)]
        if not cands:
            return None
        selector = cands[-1]
    path = os.path.join(SNAPSHOT_DIR, selector if selector.endswith(".json") else f"{selector}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["_filename"] = os.path.basename(path)
    return data

# ============ Ledger (for /trades and /pnl today) ============
def _read_ledger_rows(start_dt: datetime, end_dt: datetime, symbol: Optional[str] = None) -> List[dict]:
    _ensure_ledger()
    out = []
    try:
        with open(LEDGER_CSV, "r", encoding="utf-8") as f:
            r = csv.DictReader(f)
            for row in r:
                try:
                    ts = datetime.fromisoformat(row["ts"])
                except Exception:
                    continue
                if not (start_dt <= ts <= end_dt):
                    continue
                sym = (row.get("symbol") or "").upper()
                if symbol and sym != symbol.upper():
                    continue
                out.append({
                    "ts": ts,
                    "symbol": sym,
                    "qty": _to_dec(row.get("qty", "0")),
                    "side": (row.get("side") or "").upper(),
                    "price_usd": _to_dec(row.get("price_usd", "0")),
                    "tx": row.get("tx") or "",
                })
    except Exception:
        logging.exception("Failed reading ledger")
    out.sort(key=lambda x: x["ts"])
    return out

def _fifo_realized_pnl(rows: List[dict]) -> Tuple[Decimal, Dict[str, Decimal]]:
    per_sym_lots: Dict[str, deque] = {}
    realized_total = Decimal("0")
    realized_by_sym: Dict[str, Decimal] = {}
    for r in rows:
        sym = r["symbol"]
        qty = r["qty"]
        px  = r["price_usd"]
        side = r["side"]
        if sym not in per_sym_lots:
            per_sym_lots[sym] = deque()
        if side == "IN":
            per_sym_lots[sym].append({"qty": qty, "px": px})
        elif side == "OUT":
            to_sell = abs(qty)
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
    def _fmt_money(x: Decimal) -> str:
        try: return f"{x:,.2f}"
        except: return str(x)
    def _fmt_price(x: Decimal) -> str:
        try:
            if x >= 1000: return f"{x:,.2f}"
            if x >= 1:    return f"{x:.2f}"
            if x >= 0.01: return f"{x:.6f}"
            return f"{x:.8f}".rstrip("0").rstrip(".")
        except: return str(x)
    def _fmt_qty(x: Decimal) -> str:
        try: return f"{x:,.6f}"
        except: return str(x)

    lines = [title, "Transactions (today):"]
    net = Decimal("0")
    for r in rows:
        flow = r["qty"] * r["price_usd"] * (Decimal("1") if r["side"] == "IN" else Decimal("-1"))
        net += flow
        lines.append(
            f"• {r['ts'].strftime('%H:%M:%S')} — {r['side']} {r['symbol']} {_fmt_qty(r['qty'])} @ ${_fmt_price(r['price_usd'])}  "
            f"(${_fmt_money(flow)})"
        )
    lines.append(f"\nNet USD flow today: ${_fmt_money(net)}")
    return "\n".join(lines)

# Explorer backfill (σήμερα) → ledger (όταν άδειο)
def _explorer_backfill_today(wallet: str) -> int:
    _ensure_ledger()
    start, end = _today_range()
    wl = wallet.lower()
    wrote = 0
    # CRC20
    for r in _explorer_tokentx(wallet):
        try:
            ts = datetime.fromtimestamp(int(r.get("timeStamp", "0")), tz=ZoneInfo(TZ))
            if not (start <= ts <= end): continue
            sym = (r.get("tokenSymbol") or "").upper() or "TOKEN"
            dec = int(r.get("tokenDecimal") or 18)
            qty = _to_dec(r.get("value")) / (Decimal(10) ** dec)
            frm = (r.get("from") or "").lower()
            to  = (r.get("to") or "").lower()
            side = "IN" if to == wl and frm != wl else ("OUT" if frm == wl and to != wl else None)
            if not side: continue
            px = _to_dec(get_spot_usd(sym, token_address=(r.get("contractAddress") or "").lower()))
            with open(LEDGER_CSV, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts","symbol","qty","side","price_usd","tx"])
                if f.tell() == 0: w.writeheader()
                w.writerow({"ts": ts.isoformat(), "symbol": sym, "qty": str(qty), "side": side, "price_usd": str(px), "tx": r.get("hash") or ""})
            wrote += 1
        except Exception:
            continue
    # native CRO
    for r in _explorer_txlist(wallet):
        try:
            ts = datetime.fromtimestamp(int(r.get("timeStamp", "0")), tz=ZoneInfo(TZ))
            if not (start <= ts <= end): continue
            value_wei = _to_dec(r.get("value"))
            if value_wei <= 0: continue
            qty = value_wei / Decimal(10**18)
            frm = (r.get("from") or "").lower()
            to  = (r.get("to") or "").lower()
            side = "IN" if to == wl and frm != wl else ("OUT" if frm == wl and to != wl else None)
            if not side: continue
            px = _to_dec(get_spot_usd("CRO", token_address=None))
            with open(LEDGER_CSV, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["ts","symbol","qty","side","price_usd","tx"])
                if f.tell() == 0: w.writeheader()
                w.writerow({"ts": ts.isoformat(), "symbol": "CRO", "qty": str(qty), "side": side, "price_usd": str(px), "tx": r.get("hash") or ""})
            wrote += 1
        except Exception:
            continue
    return wrote
# ==== END app.py (Part 2/3) ==================================================
# ==== START app.py (Part 3/3) ================================================
# ============ Command handlers ============
def _handle_start() -> str:
    return (
        "👋 Γεια σου! Είμαι το Cronos DeFi Sentinel.\n\n"
        "Διαθέσιμες εντολές:\n"
        "• /holdings — snapshot χαρτοφυλακίου (compact + φίλτρα, με PnL vs τελευταίο)\n"
        "• /scan — ωμή λίστα tokens από ανακάλυψη (χωρίς φίλτρα)\n"
        "• /rescan — πλήρης επανεύρεση & εμφάνιση (με φίλτρα)\n"
        "• /snapshot — αποθήκευση snapshot με timestamp (π.χ. 2025-10-11_0930)\n"
        "• /snapshots — λίστα διαθέσιμων snapshots\n"
        "• /pnl — PnL vs snapshot [ημέρα ή stamp]  — και /pnl today [SYM]\n"
        "• /trades [SYM] — σημερινές συναλλαγές (τοπική TZ)\n"
        "• /help — βοήθεια"
    )

def _handle_help() -> str:
    return _handle_start().replace("👋 Γεια σου! Είμαι το Cronos DeFi Sentinel.\n\n", "ℹ️ Βοήθεια\n")

def _handle_scan(wallet: str) -> str:
    try:
        tokens = discover_tokens_for_wallet(wallet)
        if not tokens:
            return "🔍 Δεν βρέθηκαν ERC-20 tokens με θετικό balance (ή δεν βρέθηκαν μεταφορές στο lookback)."
        lines = ["🔍 Scan (raw):"]
        for t in tokens:
            lines.append(str(t))
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Σφάλμα στο /scan: {e}"

def _filter_and_sort_assets(assets: list) -> tuple[list, int]:
    hide_zero = (os.getenv("HOLDINGS_HIDE_ZERO_PRICE", "1").lower() in ("1","true","yes","on"))
    dust_usd  = _to_dec(os.getenv("HOLDINGS_DUST_USD", "0.05"))
    limit     = int(os.getenv("HOLDINGS_LIMIT", "40"))
    bl_re_pat = os.getenv("HOLDINGS_BLACKLIST_REGEX", r"(?i)(claim|airdrop|promo|mistery|crowithknife|classic|button|ryoshi|ethena\.promo)")
    bl_re = re.compile(bl_re_pat)
    visible, hidden = [], 0
    whitelist_zero_price = {"USDT","USDC","WCRO","CRO","WETH","WBTC","ADA","SOL","XRP","SUI","MATIC"}
    for a in assets:
        sym = str(a.get("symbol","?")).upper().strip()
        price = _to_dec(a.get("price_usd", 0))
        val   = _to_dec(a.get("value_usd", 0)) or (_to_dec(a.get("amount",0)) * price)
        if bl_re.search(sym):
            hidden += 1; continue
        if hide_zero and price <= 0 and sym not in whitelist_zero_price:
            hidden += 1; continue
        if val < dust_usd:
            hidden += 1; continue
        aa = dict(a)
        aa["price_usd"] = price
        aa["value_usd"] = val
        visible.append(aa)
    visible.sort(key=lambda x: x.get("value_usd", Decimal("0")), reverse=True)
    if limit and len(visible) > limit:
        hidden += (len(visible) - limit)
        visible = visible[:limit]
    return visible, hidden

def _format_holdings(assets: List[Dict[str, Any]], hidden_count: int) -> tuple[str, Decimal]:
    lines = ["📊 Holdings"]
    tot = Decimal("0")
    for a in assets:
        sym = str(a.get("symbol","?")).upper()
        qty = _to_dec(a.get("amount", 0))
        px  = _to_dec(a.get("price_usd", 0))
        val = _to_dec(a.get("value_usd", 0)) or (qty * px)
        tot += val
        lines.append(_asset_line(sym, qty, px, val))
    lines.append(f"\nTotal ≈ ${tot:,.2f}")
    if hidden_count:
        lines.append(f"\n(…και άλλα {hidden_count} κρυμμένα: spam/zero-price/dust)")
    return "\n".join(lines), tot

def _handle_rescan(wallet: str) -> str:
    try:
        assets, mode = _holdings_auto(wallet)
        assets, hidden = _filter_and_sort_assets(assets)
        txt, _ = _format_holdings(assets, hidden)
        return f"🔁 Rescan ({mode})\n\n{txt}"
    except Exception as e:
        return f"⚠️ Σφάλμα στο /rescan: {e}"

def _handle_holdings(wallet: str) -> str:
    try:
        assets, mode = _holdings_auto(wallet)
        assets, hidden = _filter_and_sort_assets(assets)
        txt, _ = _format_holdings(assets, hidden)
        return txt
    except Exception as e:
        return f"⚠️ Δεν μπόρεσα να εμφανίσω holdings: {e}"

def _handle_snapshot(wallet: str) -> str:
    try:
        assets, _mode = _holdings_auto(wallet)
        st = _save_snapshot(assets)
        return f"📸 Snapshot saved: {st}"
    except Exception as e:
        return f"⚠️ Σφάλμα στο /snapshot: {e}"

def _handle_snapshots() -> str:
    files = _list_snapshots(50)
    if not files:
        return "ℹ️ Δεν υπάρχουν αποθηκευμένα snapshots."
    return "📚 Snapshots:\n" + "\n".join(f" - {f[:-5]}" for f in files)

def _handle_pnl(wallet: str, selector: Optional[str]) -> str:
    snap = _load_snapshot(selector)
    if not snap:
        return "ℹ️ Δεν υπάρχουν αποθηκευμένα snapshots."
    # current total
    try:
        assets, _mode = _holdings_auto(wallet)
        curr_total = sum((_to_dec(a.get("value_usd", 0)) for a in assets), Decimal("0"))
    except Exception:
        return "⚠️ Σφάλμα κατά τον υπολογισμό PnL."
    try:
        snap_total = _to_dec((snap.get("totals") or {}).get("value_usd", 0)) or Decimal("0")
        label = str(snap.get("stamp") or snap.get("date") or "?")
    except Exception:
        snap_total, label = Decimal("0"), "?"
    delta = curr_total - snap_total
    pct = (delta / snap_total * Decimal("100")) if snap_total > 0 else Decimal("0")
    return f"💹 PnL vs {label}: Δ=${delta:,.2f}  ({pct:.2f}%)"

def _handle_trades(symbol: Optional[str] = None) -> str:
    start, end = _today_range()
    rows = _read_ledger_rows(start, end, symbol=symbol)
    return _format_trades_output(rows, "📒 Συναλλαγές (σήμερα)")

def _handle_pnl_today(symbol: Optional[str] = None) -> str:
    start, end = _today_range()
    rows = _read_ledger_rows(start, end, symbol=symbol)
    if not rows:
        # backfill (σήμερα) από explorer όταν το ledger είναι άδειο
        wrote = _explorer_backfill_today(WALLET_ADDRESS) if WALLET_ADDRESS else 0
        if wrote:
            rows = _read_ledger_rows(start, end, symbol=symbol)
    if not rows:
        return "Σήμερα δεν βρέθηκαν συναλλαγές."
    realized_total, realized_by_sym = _fifo_realized_pnl(rows)
    bysym = " ".join([f"{k}:{v:,.2f}" for k, v in realized_by_sym.items()])
    return f"💰 Realized PnL today: ${realized_total:,.2f}" + (f"  — per symbol: {bysym}" if bysym else "")

# ============ Dispatcher ============
def _dispatch_command(text: str) -> str:
    parts = (text or "").strip().split()
    if not parts:
        return ""
    cmd = parts[0].lower()
    if cmd == "/start":     return _handle_start()
    if cmd == "/help":      return _handle_help()
    if cmd == "/scan":      return _handle_scan(WALLET_ADDRESS)
    if cmd == "/rescan":    return _handle_rescan(WALLET_ADDRESS)
    if cmd == "/holdings":  return _handle_holdings(WALLET_ADDRESS)
    if cmd == "/snapshot":  return _handle_snapshot(WALLET_ADDRESS)
    if cmd == "/snapshots": return _handle_snapshots()
    if cmd == "/pnl":
        # /pnl today [SYM]  OR  /pnl [YYYY-MM-DD|YYYY-MM-DD_HHMM]
        if len(parts) >= 2 and parts[1].lower() == "today":
            sym = parts[2].upper() if len(parts) >= 3 else None
            return _handle_pnl_today(sym)
        arg = parts[1] if len(parts) > 1 else None
        return _handle_pnl(WALLET_ADDRESS, arg)
    if cmd == "/trades":
        sym = parts[1].upper() if len(parts) >= 2 else None
        return _handle_trades(sym)
    return "❓ Άγνωστη εντολή. Δοκίμασε /help."

# ============ Webhook ============
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
            if reply:
                send_message(reply, chat_id)
    except Exception:
        logging.exception("Error handling Telegram webhook")
    return JSONResponse(content={"ok": True})

# ============ Health ============
@app.get("/healthz")
async def healthz():
    return JSONResponse(content={"ok": True, "service": "wallet_monitor_dex", "status": "running"})

# ============ Startup ============
@app.on_event("startup")
async def on_startup():
    logging.info("✅ Cronos DeFi Sentinel started and is online.")
    _ensure_dir("./data")
    _ensure_dir(SNAPSHOT_DIR)
    _ensure_ledger()
    try:
        if CHAT_ID:
            send_message("✅ Cronos DeFi Sentinel started and is online.", CHAT_ID)
    except Exception:
        pass

    # Boot live wallet monitor (separate loop implemented in monitor.py)
    if MONITOR_ENABLE and WALLET_ADDRESS:
        async def _runner():
            # monitor.py κάνει explorer polling & γράφει στο ledger
            await rt_monitor.run_once_forever(poll_seconds=RT_POLL_SEC)

        async def _supervisor():
            while True:
                try:
                    await _runner()
                except Exception as e:
                    logging.exception(f"monitor crashed: {e}")
                    await asyncio.sleep(3)

        asyncio.create_task(_supervisor())
# ==== END app.py (Part 3/3) ==================================================
