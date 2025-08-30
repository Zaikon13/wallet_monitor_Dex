#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner
+ Auto Discovery + PnL (realized & unrealized) reports
Plug-and-play. Uses EXACT Railway env var names you already set.
"""

import os
import time
import threading
from collections import deque, defaultdict
import json
import math
from datetime import datetime, timedelta
import requests
from dotenv import load_dotenv

load_dotenv()

# ========================= Environment (exact names) =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")       # Etherscan Multichain (used with chainid=25)
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")       # optional
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5.0"))
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL           = int(os.getenv("DEX_POLL", "60"))

# Optional
PRICE_WINDOW          = int(os.getenv("PRICE_WINDOW", "3"))
SPIKE_THRESHOLD       = float(os.getenv("SPIKE_THRESHOLD", "8.0"))
MIN_VOLUME_FOR_ALERT  = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

DISCOVER_ENABLED      = os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on")
DISCOVER_QUERY        = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT        = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL         = int(os.getenv("DISCOVER_POLL", "120"))
TOKENS                = os.getenv("TOKENS", "")

# Reporting schedule
TZ                    = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS        = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR              = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE            = int(os.getenv("EOD_MINUTE", "59"))

# ========================= Constants =========================
ETHERSCAN_V2_URL   = "https://api.etherscan.io/v2/api"            # multichain endpoint (needs chainid)
CRONOS_CHAINID     = 25
DEX_BASE_PAIRS     = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS    = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH    = "https://api.dexscreener.com/latest/dex/search"
DEXSITE_PAIR       = "https://dexscreener.com/{chain}/{pair}"     # for user links
CRONOS_TX          = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL       = "https://api.telegram.org/bot{token}/sendMessage"

DATA_DIR           = "/app/data"

# Apply timezone (best effort)
try:
    os.environ["TZ"] = TZ
    if hasattr(time, "tzset"):
        time.tzset()
except Exception:
    pass

# Global session
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
})

# ========================= State =========================
_seen_tx_hashes   = set()
_last_prices      = {}            # slug -> float
_price_history    = {}            # slug -> deque
_last_pair_tx     = {}            # slug -> tx hash
_rate_limit_last  = 0.0
_tracked_pairs    = set()
_known_pairs_meta = {}

_day_ledger_lock  = threading.Lock()
_token_balances   = defaultdict(float)
_token_meta       = {}
_position_qty     = defaultdict(float)
_position_cost    = defaultdict(float)
_realized_pnl_today = 0.0
EPSILON           = 1e-12
_last_intraday_sent = 0.0

PRICE_CACHE = {}
PRICE_CACHE_TTL = 60

try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

# ========================= Utils =========================
def now_dt():
    return datetime.now()

def ymd(dt=None):
    if dt is None:
        dt = now_dt()
    return dt.strftime("%Y-%m-%d")

def month_prefix(dt=None):
    if dt is None:
        dt = now_dt()
    return dt.strftime("%Y-%m")

def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def send_telegram(message: str) -> bool:
    global _rate_limit_last
    now = time.time()
    if now - _rate_limit_last < 0.8:
        time.sleep(0.8 - (now - _rate_limit_last))

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID).")
        return False

    url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        r = SESSION.post(url, data=payload, timeout=12)
        _rate_limit_last = time.time()
        if r.status_code == 401:
            print("❌ Telegram 401 Unauthorized. Regenerate token.")
            raise SystemExit(1)
        if r.status_code != 200:
            print("⚠️ Telegram API returned", r.status_code, r.text[:300])
            return False
        return True
    except SystemExit:
        raise
    except Exception as e:
        print("Exception sending telegram:", e)
        return False

def safe_json(r):
    if r is None:
        return None
    if not getattr(r, "ok", False):
        print("HTTP error:", getattr(r, "status_code", None), str(getattr(r, "text", ""))[:300])
        return None
    try:
        return r.json()
    except Exception:
        txt = (r.text[:800].replace("\n", " ")) if hasattr(r, "text") else "<no body>"
        print("Response not JSON (preview):", txt)
        return None

# ----------------- Price helpers (Dexscreener + CoinGecko fallback) -----------------
def _format_price(p):
    try:
        p = float(p)
    except Exception:
        return str(p)
    return f"{p:,.6f}"

def _nonzero(v, eps=1e-12):
    try:
        return abs(float(v)) > eps
    except Exception:
        return False

def _top_price_from_pairs_pricehelpers(pairs):
    if not pairs:
        return None
    best = None
    best_liq = -1.0
    for p in pairs:
        try:
            chain_id = str(p.get("chainId","")).lower()
            if chain_id and chain_id != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq = liq
                best = price
        except Exception:
            continue
    return best

def _price_from_dexscreener_token(token_addr):
    try:
        url = f"{DEX_BASE_TOKENS}/cronos/{token_addr}"
        r = SESSION.get(url, timeout=12)
        data = safe_json(r)
        if not data:
            return None
        pairs = data.get("pairs") if isinstance(data, dict) else None
        return _top_price_from_pairs_pricehelpers(pairs)
    except Exception as e:
        print("Error _price_from_dexscreener_token:", e)
        return None

def _price_from_dexscreener_search(symbol_or_query):
    try:
        r = SESSION.get(DEX_BASE_SEARCH, params={"q": symbol_or_query}, timeout=12)
        data = safe_json(r)
        if not data:
            return None
        pairs = data.get("pairs") if isinstance(data, dict) else None
        return _top_price_from_pairs_pricehelpers(pairs)
    except Exception as e:
        print("Error _price_from_dexscreener_search:", e)
        return None

def _price_from_coingecko_contract(token_addr):
    try:
        addr = token_addr.lower()
        url = "https://api.coingecko.com/api/v3/simple/token_price/cronos"
        params = {"contract_addresses": addr, "vs_currencies": "usd"}
        r = SESSION.get(url, params=params, timeout=12)
        data = safe_json(r)
        if not data:
            return None
        val = None
        if isinstance(data, dict):
            v = data.get(addr)
            if v and "usd" in v:
                val = v["usd"]
            else:
                for k, vv in data.items():
                    if k.lower() == addr and isinstance(vv, dict) and "usd" in vv:
                        val = vv["usd"]
                        break
        if val is not None:
            try:
                return float(val)
            except Exception:
                return None
    except Exception as e:
        print("Error _price_from_coingecko_contract:", e)
    return None

def _price_from_coingecko_ids_for_cro():
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        ids = "cronos,crypto-com-chain"
        r = SESSION.get(url, params={"ids": ids, "vs_currencies": "usd"}, timeout=8)
        data = safe_json(r)
        if not data:
            return None
        for idk in ("cronos", "crypto-com-chain"):
            if idk in data and "usd" in data[idk]:
                try:
                    return float(data[idk]["usd"])
                except Exception:
                    continue
    except Exception as e:
        print("Error _price_from_coingecko_ids_for_cro:", e)
    return None

def get_price_usd(symbol_or_addr: str):
    """
    Robust price fetch with cache:
      - accepts "CRO"/"cro" or ERC20 contract "0x..."
      - Dexscreener tokens endpoint (best), then search, then CoinGecko fallbacks
    """
    if not symbol_or_addr:
        return None
    key = symbol_or_addr.strip().lower()

    now_ts = time.time()
    cached = PRICE_CACHE.get(key)
    if cached:
        price, ts = cached
        if now_ts - ts < PRICE_CACHE_TTL:
            return price

    price = None
    if key in ("cro", "wcro", "w-cro", "wrappedcro", "wrapped cro"):
        price = _price_from_dexscreener_search("cro usdt") or _price_from_dexscreener_search("wcro usdt")
        if not price:
            price = _price_from_coingecko_ids_for_cro()
    elif key.startswith("0x") and len(key) == 42:
        price = _price_from_dexscreener_token(key)
        if not price:
            price = _price_from_coingecko_contract(key)
        if not price:
            price = _price_from_dexscreener_search(key)
    else:
        price = _price_from_dexscreener_search(key)
        if not price and len(key) <= 8:
            price = _price_from_dexscreener_search(f"{key} usdt")

    try:
        PRICE_CACHE[key] = (price, now_ts)
    except Exception:
        pass

    if price is None:
        print(f"Price lookup failed for '{symbol_or_addr}' (returned None).")
    return price

# --------- NEW robust token price fetch (fallbacks, avoids 404) ----------
def get_token_price(chain: str, token_address: str):
    """
    Robust get price for token: tries multiple Dexscreener endpoints then CoinGecko fallback.
    Returns float (0.0 on not found).
    """
    try:
        # 1) Try token pairs endpoint (chain/address)
        url = f"https://api.dexscreener.com/latest/dex/tokens/{chain}/{token_address}"
        r = SESSION.get(url, timeout=10)
        if r.status_code == 200:
            d = safe_json(r)
            if d and isinstance(d, dict) and "pairs" in d and d["pairs"]:
                p = _top_price_from_pairs_pricehelpers(d["pairs"])
                if p:
                    return float(p)

        # 2) Fallback: token-only endpoint (some tokens)
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = SESSION.get(url, timeout=10)
        if r.status_code == 200:
            d = safe_json(r)
            if d and isinstance(d, dict) and "pairs" in d and d["pairs"]:
                p = _top_price_from_pairs_pricehelpers(d["pairs"])
                if p:
                    return float(p)

        # 3) Try search
        p = _price_from_dexscreener_search(token_address)
        if p:
            return float(p)

        # 4) CoinGecko fallback for contract on Cronos
        try:
            cg = _price_from_coingecko_contract(token_address)
            if cg:
                return float(cg)
        except Exception:
            pass

        # 5) Not found: return 0.0 (caller will handle)
        return 0.0
    except Exception as e:
        print(f"[get_token_price] Error for {token_address}: {e}")
        return 0.0

# ========================= Wallet (Cronos via Etherscan Multichain) =========================
def fetch_latest_wallet_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API:
        print("Missing WALLET_ADDRESS or ETHERSCAN_API.")
        return []
    params = {
        "chainid": CRONOS_CHAINID,
        "module": "account",
        "action": "txlist",
        "address": WALLET_ADDRESS,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": ETHERSCAN_API,
    }
    try:
        r = SESSION.get(ETHERSCAN_V2_URL, params=params, timeout=15)
        data = safe_json(r)
        if not data:
            return []
        if str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
            return data["result"]
        print("Unexpected wallet response:", str(data)[:600])
        return []
    except Exception as e:
        print("Error fetching wallet txs:", e)
        return []

def fetch_latest_token_txs(limit=50):
    if not WALLET_ADDRESS or not ETHERSCAN_API:
        return []
    params = {
        "chainid": CRONOS_CHAINID,
        "module": "account",
        "action": "tokentx",
        "address": WALLET_ADDRESS,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": ETHERSCAN_API,
    }
    try:
        r = SESSION.get(ETHERSCAN_V2_URL, params=params, timeout=15)
        data = safe_json(r)
        if not data:
            return []
        if str(data.get("status", "")).strip() == "1" and isinstance(data.get("result"), list):
            return data["result"]
        print("Unexpected token tx response:", str(data)[:600])
        return []
    except Exception as e:
        print("Error fetching token txs:", e)
        return []

def _append_ledger(entry: dict):
    with _day_ledger_lock:
        path = data_file_for_today()
        data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        data["entries"].append(entry)
        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
        data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0))
        write_json(path, data)

def _format_amount(a):
    if a is None:
        return "0"
    try:
        a = float(a)
    except Exception:
        return str(a)
    if abs(a) >= 1:
        return f"{a:,.4f}"
    if abs(a) >= 0.0001:
        return f"{a:.6f}"
    return f"{a:.8f}"

# ----------------- Wallet snapshot helper (Cronoscan token list) ------------
CRONOSCAN_API = os.getenv("CRONOSCAN_API")  # optional; if not present fallback to internal balances

def get_wallet_balances_snapshot(address):
    """
    Try Cronoscan token list endpoint (if CRONOSCAN_API provided).
    Fallback: return current in-memory _token_balances (from token tx processing).
    Returns dict: symbol -> amount (float). Also includes CRO as 'CRO'.
    """
    balances = {}
    addr = (address or WALLET_ADDRESS or "").lower()
    # 1) Try Cronoscan token list (some nodes / explorers provide token list)
    if CRONOSCAN_API:
        try:
            url = f"https://api.cronoscan.com/api"
            params = {"module": "account", "action": "tokenlist", "address": addr, "apikey": CRONOSCAN_API}
            r = SESSION.get(url, params=params, timeout=10)
            data = safe_json(r)
            if isinstance(data, dict) and data.get("status") in ("1","0") and isinstance(data.get("result"), list):
                for tok in data.get("result", []):
                    sym = tok.get("symbol") or tok.get("tokenSymbol") or tok.get("name") or ""
                    try:
                        dec = int(tok.get("decimals") or tok.get("tokenDecimal") or 18)
                    except Exception:
                        dec = 18
                    bal_raw = tok.get("balance") or tok.get("tokenBalance") or "0"
                    try:
                        amt = int(bal_raw) / (10 ** dec)
                    except Exception:
                        try:
                            amt = float(bal_raw)
                        except Exception:
                            amt = 0.0
                    if sym:
                        balances[sym] = balances.get(sym, 0.0) + amt
            # Always include CRO native balance if present in response
            # Some explorers include native balance as 'native' or similar - but not guaranteed
        except Exception as e:
            print("Cronoscan snapshot error:", e)
            balances = {}
    # 2) Fallback to in-memory balances collected by monitor
    if not balances:
        # convert internal _token_balances (keys may be token_addr or "CRO") to symbol-keyed map
        for k, v in list(_token_balances.items()):
            if k == "CRO":
                balances["CRO"] = balances.get("CRO", 0.0) + v
            else:
                meta = _token_meta.get(k, {})
                sym = meta.get("symbol") or k[:8]
                balances[sym] = balances.get(sym, 0.0) + v
    # ensure CRO key exists (even if zero)
    if "CRO" not in balances:
        balances["CRO"] = float(_token_balances.get("CRO", 0.0))
    return balances

# ----------------- Compute holdings / MTM / unrealized --------------------
def compute_holdings_usd():
    """
    Returns (total_usd, breakdown list, unrealized_pnl)
    breakdown items: {"token","token_addr","amount","price_usd","usd_value"}
    - Shows only positive balances
    - Uses get_price_usd/get_token_price to fetch prices (robust)
    """
    total = 0.0
    breakdown = []
    unrealized = 0.0

    # CRO native
    cro_amt = max(0.0, _token_balances.get("CRO", 0.0))
    if cro_amt > EPSILON:
        cro_price = get_price_usd("CRO") or 0.0
        cro_val = cro_amt * cro_price
        total += cro_val
        breakdown.append({"token": "CRO", "token_addr": None, "amount": cro_amt, "price_usd": cro_price, "usd_value": cro_val})
        rem_qty = _position_qty.get("CRO", 0.0)
        rem_cost= _position_cost.get("CRO", 0.0)
        if rem_qty > EPSILON:
            unrealized += (cro_val - rem_cost)

    # other tokens tracked in _token_balances (keys are token_addr or other)
    for addr, amt in list(_token_balances.items()):
        if addr == "CRO":
            continue
        amt = max(0.0, amt)
        if amt <= EPSILON:
            continue
        meta = _token_meta.get(addr, {})
        sym = meta.get("symbol") or addr[:8]
        # prefer token contract price fetch if addr looks like 0x...
        price = None
        if isinstance(addr, str) and addr.startswith("0x") and len(addr) == 42:
            price = get_token_price("cronos", addr) or get_price_usd(addr)
        else:
            price = get_price_usd(sym) or get_price_usd(addr)
        price = price or 0.0
        val = amt * price
        total += val
        breakdown.append({"token": sym, "token_addr": addr, "amount": amt, "price_usd": price, "usd_value": val})
        rem_qty = _position_qty.get(addr, 0.0)
        rem_cost= _position_cost.get(addr, 0.0)
        if rem_qty > EPSILON:
            unrealized += (val - rem_cost)
    return total, breakdown, unrealized

# ----------------- Month aggregates ---------------------------------------
def sum_month_net_flows_and_realized():
    pref = month_prefix()
    total_flow = 0.0
    total_real = 0.0
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json") and pref in fn:
                data = read_json(os.path.join(DATA_DIR, fn), default=None)
                if isinstance(data, dict):
                    total_flow += float(data.get("net_usd_flow", 0.0))
                    total_real += float(data.get("realized_pnl", 0.0))
    except Exception:
        pass
    return total_flow, total_real

# ----------------- Day report builder (enhanced with per-asset summary) ----
def build_day_report_text():
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])
    net_flow = float(data.get("net_usd_flow", 0.0))
    realized_today = float(data.get("realized_pnl", 0.0))

    per_asset_flow = defaultdict(float)
    per_asset_real = defaultdict(float)
    per_asset_amt  = defaultdict(float)
    per_asset_last_price = {}
    per_asset_token_addr = {}

    for e in entries:
        tok = e.get("token") or "?"
        addr = e.get("token_addr")
        per_asset_flow[tok] += float(e.get("usd_value") or 0.0)
        per_asset_real[tok] += float(e.get("realized_pnl") or 0.0)
        per_asset_amt[tok]  += float(e.get("amount") or 0.0)
        if _nonzero(e.get("price_usd", 0.0)):
            per_asset_last_price[tok] = float(e.get("price_usd"))
        if addr and tok not in per_asset_token_addr:
            per_asset_token_addr[tok] = addr

    lines = [f"*📒 Daily Report* ({data.get('date')})"]
    if not entries:
        lines.append("_No transactions today._")
    else:
        lines.append("*Transactions:*")
        MAX_LINES = 20
        for i, e in enumerate(entries[-MAX_LINES:]):
            tok = e.get("token") or "?"
            amt = e.get("amount") or 0
            usd = e.get("usd_value") or 0
            tm  = e.get("time","")[-8:]
            direction = "IN" if float(amt) > 0 else "OUT"
            unit_price = e.get("price_usd") or 0.0
            pnl_line = ""
            try:
                rp = float(e.get("realized_pnl", 0.0))
                if abs(rp) > 1e-9:
                    pnl_line = f"  PnL: ${_format_amount(rp)}"
            except Exception:
                pass
            lines.append(
                f"• {tm} — {direction} {tok} {_format_amount(amt)}  "
                f"@ ${_format_price(unit_price)}  "
                f"(${_format_amount(usd)}){pnl_line}"
            )
        if len(entries) > MAX_LINES:
            lines.append(f"_…and {len(entries)-MAX_LINES} earlier txs._")

    lines.append(f"\n*Net USD flow today:* ${_format_amount(net_flow)}")
    lines.append(f"*Realized PnL today:* ${_format_amount(realized_today)}")

    holdings_total, breakdown, unrealized = compute_holdings_usd()
    lines.append(f"*Holdings (MTM) now:* ${_format_amount(holdings_total)}")
    if breakdown:
        for b in breakdown[:15]:
            tok = b['token']
            lines.append(
                f"  – {tok}: {_format_amount(b['amount'])} @ ${_format_price(b['price_usd'])} = ${_format_amount(b['usd_value'])}"
            )
        if len(breakdown) > 15:
            lines.append(f"  …and {len(breakdown)-15} more.")
    lines.append(f"*Unrealized PnL (open positions):* ${_format_amount(unrealized)}")

    # Per-asset summary
    if per_asset_flow:
        lines.append("\n*Per-Asset Summary (Today):*")
        order = sorted(per_asset_flow.items(), key=lambda kv: abs(kv[1]), reverse=True)
        LIMIT = 12
        for idx, (tok, flow) in enumerate(order[:LIMIT]):
            addr = per_asset_token_addr.get(tok)
            live_price = per_asset_last_price.get(tok)
            if live_price is None:
                if tok.upper() == "CRO":
                    live_price = get_price_usd("CRO") or 0.0
                elif addr and isinstance(addr, str) and addr.startswith("0x"):
                    live_price = get_token_price("cronos", addr) or 0.0
                else:
                    live_price = get_price_usd(tok) or 0.0
            amt_sum = per_asset_amt.get(tok, 0.0)
            real_sum = per_asset_real.get(tok, 0.0)
            lines.append(
                f"  • {tok}: flow ${_format_amount(flow)} | realized ${_format_amount(real_sum)} | "
                f"today qty {_format_amount(amt_sum)} | price ${_format_price(live_price)}"
            )
        if len(order) > LIMIT:
            lines.append(f"  …and {len(order)-LIMIT} more.")

    month_flow, month_real = sum_month_net_flows_and_realized()
    lines.append(f"\n*Month Net Flow:* ${_format_amount(month_flow)}")
    lines.append(f"*Month Realized PnL:* ${_format_amount(month_real)}")

    return "\n".join(lines)

# ----------------- Intraday & EOD report scheduling -----------------------
def intraday_report_loop():
    global _last_intraday_sent
    time.sleep(10)
    send_telegram("⏱ Intraday reporting enabled.")
    while True:
        if time.time() - _last_intraday_sent >= INTRADAY_HOURS * 3600:
            try:
                txt = build_day_report_text()
                send_telegram("🟡 *Intraday Update*\n" + txt)
            except Exception as e:
                print("Intraday report error:", e)
            _last_intraday_sent = time.time()
        time.sleep(30)

def end_of_day_scheduler_loop():
    send_telegram(f"🕛 End-of-day scheduler active (at {EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ}).")
    while True:
        now = now_dt()
        target = now.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)
        if now > target:
            target = target + timedelta(days=1)
        wait_s = (target - now).total_seconds()
        while wait_s > 0:
            s = min(wait_s, 30)
            time.sleep(s)
            wait_s -= s
        try:
            txt = build_day_report_text()
            send_telegram("🟢 *End of Day Report*\n" + txt)
        except Exception as e:
            print("EOD report error:", e)

# ----------------- Dexscreener monitor core --------------------------------
def slug(chain: str, pair_address: str) -> str:
    return f"{chain}/{pair_address}".lower()

def fetch_pair(slug_str: str):
    url = f"{DEX_BASE_PAIRS}/{slug_str}"
    try:
        r = SESSION.get(url, timeout=12)
        return safe_json(r)
    except Exception as e:
        print("Error fetching pair", slug_str, e)
        return None

def fetch_token_pairs(chain: str, token_address: str):
    url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
    try:
        r = SESSION.get(url, timeout=12)
        data = safe_json(r)
        if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
            return data["pairs"]
        return []
    except Exception as e:
        print("Error fetching token", chain, token_address, e)
        return []

def fetch_search(query: str):
    try:
        r = SESSION.get(DEX_BASE_SEARCH, params={"q": query}, timeout=15)
        data = safe_json(r)
        if isinstance(data, dict) and "pairs" in data and isinstance(data["pairs"], list):
            return data["pairs"]
        return []
    except Exception as e:
        print("Error search dexscreener:", e)
        return []

def ensure_tracking_pair(chain: str, pair_address: str, meta: dict = None):
    s = slug(chain, pair_address)
    if s not in _tracked_pairs:
        _tracked_pairs.add(s)
        _last_prices[s]   = None
        _last_pair_tx[s]  = None
        _price_history[s] = deque(maxlen=PRICE_WINDOW)
        if meta:
            _known_pairs_meta[s] = meta
        ds_link = DEXSITE_PAIR.format(chain=chain, pair=pair_address)
        sym = None
        if isinstance(meta, dict):
            bt = meta.get("baseToken") or {}
            sym = bt.get("symbol")
        title = f"{sym} ({s})" if sym else s
        send_telegram(f"🆕 Now monitoring pair: {title}\n{ds_link}")

def update_price_history(slg, price):
    hist = _price_history.get(slg)
    if hist is None:
        hist = deque(maxlen=PRICE_WINDOW)
        _price_history[slg] = hist
    hist.append(price)
    _last_prices[slg] = price

def detect_spike(slg):
    hist = _price_history.get(slg)
    if not hist or len(hist) < 2:
        return None
    first = hist[0]
    last  = hist[-1]
    if not first:
        return None
    pct = (last - first) / first * 100.0
    return pct if abs(pct) >= SPIKE_THRESHOLD else None

def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        print("No tracked pairs; monitor waits until discovery/seed adds some.")
    else:
        send_telegram(f"🚀 Dexscreener monitor started for: {', '.join(sorted(_tracked_pairs))}")

    while True:
        if not _tracked_pairs:
            time.sleep(DEX_POLL)
            continue

        for s in list(_tracked_pairs):
            data = fetch_pair(s)
            if not data:
                continue

            pair = None
            if isinstance(data, dict) and isinstance(data.get("pair"), dict):
                pair = data["pair"]
            elif isinstance(data, dict) and isinstance(data.get("pairs"), list) and data["pairs"]:
                pair = data["pairs"][0]
            else:
                print("Unexpected dexscreener format for", s)
                continue

            price_val = None
            try:
                price_val = float(pair.get("priceUsd") or 0)
            except Exception:
                price_val = None

            vol_h1 = None
            vol = pair.get("volume") or {}
            if isinstance(vol, dict):
                try:
                    vol_h1 = float(vol.get("h1") or 0)
                except Exception:
                    vol_h1 = None

            symbol = (pair.get("baseToken") or {}).get("symbol") or s

            if price_val is not None and price_val > 0:
                update_price_history(s, price_val)
                spike_pct = detect_spike(s)
                if spike_pct is not None:
                    if MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 < MIN_VOLUME_FOR_ALERT:
                        pass
                    else:
                        send_telegram(
                            f"🚨 Spike on {symbol}: {spike_pct:.2f}% over last {len(_price_history[s])} samples\n"
                            f"Price: ${price_val:.6f} Vol1h: {vol_h1}"
                        )
                        _price_history[s].clear()
                        _last_prices[s] = price_val

            prev = _last_prices.get(s)
            if prev is not None and price_val is not None and prev != 0:
                delta = (price_val - prev) / prev * 100.0
                if abs(delta) >= PRICE_MOVE_THRESHOLD:
                    send_telegram(
                        f"📈 Price move on {symbol}: {delta:.2f}%\n"
                        f"Price: ${price_val:.6f} (prev ${prev:.6f})"
                    )
                    _last_prices[s] = price_val

            last_tx = pair.get("lastTx") or {}
            last_tx_hash = last_tx.get("hash") if isinstance(last_tx, dict) else None
            if last_tx_hash:
                prev_tx = _last_pair_tx.get(s)
                if prev_tx != last_tx_hash:
                    _last_pair_tx[s] = last_tx_hash
                    send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx_hash)}")

        time.sleep(DEX_POLL)

# ----------------- Auto discovery (wallet activity-based) -----------------
def discovery_loop():
    # seed from TOKENS env (optional)
    seeds = [p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    for s in seeds:
        if s.startswith("cronos/"):
            ensure_tracking_pair("cronos", s.split("/",1)[1])

    token_items = [t.strip().lower() for t in (TOKENS or "").split(",") if t.strip()]
    for t in token_items:
        if not t.startswith("cronos/"):
            continue
        _, token_addr = t.split("/", 1)
        pairs = fetch_token_pairs("cronos", token_addr)
        if pairs:
            p = pairs[0]
            pair_addr = p.get("pairAddress")
            if pair_addr:
                ensure_tracking_pair("cronos", pair_addr, meta=p)

    if not DISCOVER_ENABLED:
        print("Discovery disabled (DISCOVER_ENABLED=false).")
        return

    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos).")

    while True:
        try:
            found = fetch_search(DISCOVER_QUERY)
            adopted = 0
            for p in found or []:
                if str(p.get("chainId", "")).lower() != "cronos":
                    continue
                pair_addr = p.get("pairAddress")
                if not pair_addr:
                    continue
                s = slug("cronos", pair_addr)
                if s in _tracked_pairs:
                    continue
                ensure_tracking_pair("cronos", pair_addr, meta=p)
                adopted += 1
                if adopted >= DISCOVER_LIMIT:
                    break
        except Exception as e:
            print("Discovery error:", e)

        time.sleep(DISCOVER_POLL)

# ----------------- Reconciliation (basic swap recon) -----------------------
def reconcile_swaps_from_entries():
    """
    Basic pass to try to match token in/out pairs into swap-like logical trades.
    This is a small reconciliation helper used for reporting; advanced cases need on-chain trace.
    """
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])
    # simplistic: group consecutive OUT then IN within small time window -> treat as swap
    swaps = []
    i = 0
    while i < len(entries):
        e = entries[i]
        if float(e.get("amount", 0.0)) < 0:
            # look ahead for positive entry soon with opposite token
            j = i + 1
            while j < len(entries) and j <= i + 6:
                e2 = entries[j]
                # if opposite sign and different token -> possible swap
                if float(e2.get("amount", 0.0)) > 0 and e2.get("token") != e.get("token"):
                    swaps.append((e, e2))
                    break
                j += 1
        i += 1
    # We don't mutate ledger here; just return found swaps for debug/report usage
    return swaps

# ----------------- Entrypoint & threads ------------------------------------
def monitor_tracked_pairs_loop_wrapper():
    try:
        monitor_tracked_pairs_loop()
    except Exception as e:
        print("Pairs monitor crashed:", e)
        time.sleep(3)

def main():
    print("Starting monitor with config:")
    print("WALLET_ADDRESS:", WALLET_ADDRESS)
    print("TELEGRAM_BOT_TOKEN present:", bool(TELEGRAM_BOT_TOKEN))
    print("TELEGRAM_CHAT_ID:", TELEGRAM_CHAT_ID)
    print("ETHERSCAN_API present:", bool(ETHERSCAN_API))
    print("DEX_PAIRS:", DEX_PAIRS)
    print("DISCOVER_ENABLED:", DISCOVER_ENABLED, "| DISCOVER_QUERY:", DISCOVER_QUERY)
    print("TZ:", TZ, "| INTRADAY_HOURS:", INTRADAY_HOURS, "| EOD:", f"{EOD_HOUR:02d}:{EOD_MINUTE:02d}")

    t_wallet  = threading.Thread(target=wallet_monitor_loop, daemon=True)
    t_pairs   = threading.Thread(target=monitor_tracked_pairs_loop_wrapper, daemon=True)
    t_discover= threading.Thread(target=discovery_loop, daemon=True)
    t_intraday= threading.Thread(target=intraday_report_loop, daemon=True)
    t_eod     = threading.Thread(target=end_of_day_scheduler_loop, daemon=True)

    t_wallet.start()
    t_pairs.start()
    t_discover.start()
    t_intraday.start()
    t_eod.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("Stopping monitors.")

if __name__ == "__main__":
    main()
