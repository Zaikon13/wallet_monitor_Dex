#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Wallet Monitor για Cronos (Etherscan v2 Multichain) + Dexscreener
- Auto-discovery με φίλτρα (liq/vol/change/age), auto-adopt pairs
- PnL (realized & unrealized) + σωστό aggregation ανά asset (contract-first)
- Intraday/EOD reports, mini-summary σε κάθε buy/sell
- ATH tracking (per token)
- Swap reconciliation (basic)
- Alerts:
  * 24h pump/dump για ΟΛΑ τα assets που κρατάς (interval 15’)
  * Guard μετά από κάθε buy: +pump/-dump/trailing από peak
- Rate-limit & retry/backoff για 404/429
- Χωρίς εξάρτηση από Cronoscan (προαιρετικό, απενεργοποιημένο by default)

ENV που χρησιμοποιεί:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  WALLET_ADDRESS, ETHERSCAN_API
  # Προαιρετικά (auto-discovery):
  DISCOVER_ENABLED=true|false
  DISCOVER_QUERY=cronos
  DISCOVER_LIMIT=10
  DISCOVER_POLL=120
  DISCOVER_MIN_LIQ_USD=30000
  DISCOVER_MIN_VOL24_USD=5000
  DISCOVER_MIN_ABS_CHANGE_PCT=10
  DISCOVER_MAX_PAIR_AGE_HOURS=24
  DISCOVER_REQUIRE_WCRO=true|false
  DISCOVER_BASE_WHITELIST= (comma symbols)
  DISCOVER_BASE_BLACKLIST= (comma symbols)
  # Reports:
  TZ=Europe/Athens
  INTRADAY_HOURS=3
  EOD_HOUR=23
  EOD_MINUTE=59
  # Dex monitor thresholds:
  PRICE_WINDOW=3
  PRICE_MOVE_THRESHOLD=5
  SPIKE_THRESHOLD=8
  MIN_VOLUME_FOR_ALERT=0
  # Alerts (24h wallet):
  ALERTS_INTERVAL_MIN=15
  DUMP_ALERT_24H_PCT=-15
  PUMP_ALERT_24H_PCT=20
  # Guard μετά από buy:
  GUARD_WINDOW_MIN=60
  GUARD_PUMP_PCT=20
  GUARD_DROP_PCT=-12
  GUARD_TRAIL_DROP_PCT=-8
  # Optional seeds:
  TOKENS=cronos/0x...,cronos/0x...
  DEX_PAIRS=cronos/0x...,cronos/0x...
"""

import os, sys, time, json, signal, threading, logging
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone
import math
import requests
from dotenv import load_dotenv

load_dotenv()

# ----------------------- Config / ENV -----------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API") or ""

# Optional seeds (still supported)
TOKENS      = os.getenv("TOKENS", "")         # e.g. "cronos/0xabc,cronos/0xdef"
DEX_PAIRS   = os.getenv("DEX_PAIRS", "")      # e.g. "cronos/0xpair1,cronos/0xpair2"

# Poll/monitor settings
WALLET_POLL = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL    = int(os.getenv("DEX_POLL", "60"))
PRICE_WINDOW= int(os.getenv("PRICE_WINDOW","3"))
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD","5"))
SPIKE_THRESHOLD      = float(os.getenv("SPIKE_THRESHOLD","8"))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT","0"))

# Discovery
DISCOVER_ENABLED  = (os.getenv("DISCOVER_ENABLED","true").lower() in ("1","true","yes","on"))
DISCOVER_QUERY    = os.getenv("DISCOVER_QUERY","cronos")
DISCOVER_LIMIT    = int(os.getenv("DISCOVER_LIMIT","10"))
DISCOVER_POLL     = int(os.getenv("DISCOVER_POLL","120"))

# Discovery Filters (NEW)
DISCOVER_MIN_LIQ_USD       = float(os.getenv("DISCOVER_MIN_LIQ_USD","30000"))
DISCOVER_MIN_VOL24_USD     = float(os.getenv("DISCOVER_MIN_VOL24_USD","5000"))
DISCOVER_MIN_ABS_CHANGE_PCT= float(os.getenv("DISCOVER_MIN_ABS_CHANGE_PCT","10"))
DISCOVER_MAX_PAIR_AGE_HOURS= int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS","24"))
DISCOVER_REQUIRE_WCRO      = (os.getenv("DISCOVER_REQUIRE_WCRO","false").lower() in ("1","true","yes","on"))
DISCOVER_BASE_WHITELIST    = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_WHITELIST","").split(",") if s.strip()]
DISCOVER_BASE_BLACKLIST    = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_BLACKLIST","").split(",") if s.strip()]

# Time / reports
TZ          = os.getenv("TZ","Europe/Athens")
INTRADAY_HOURS = int(os.getenv("INTRADAY_HOURS","3"))
EOD_HOUR    = int(os.getenv("EOD_HOUR","23"))
EOD_MINUTE  = int(os.getenv("EOD_MINUTE","59"))

# Alerts (24h wallet)
ALERTS_INTERVAL_MIN = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
DUMP_ALERT_24H_PCT  = float(os.getenv("DUMP_ALERT_24H_PCT","-15"))
PUMP_ALERT_24H_PCT  = float(os.getenv("PUMP_ALERT_24H_PCT","20"))

# Guard after buy
GUARD_WINDOW_MIN    = int(os.getenv("GUARD_WINDOW_MIN","60"))
GUARD_PUMP_PCT      = float(os.getenv("GUARD_PUMP_PCT","20"))
GUARD_DROP_PCT      = float(os.getenv("GUARD_DROP_PCT","-12"))
GUARD_TRAIL_DROP_PCT= float(os.getenv("GUARD_TRAIL_DROP_PCT","-8"))

# ----------------------- Constants -----------------------
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL     = "https://api.telegram.org/bot{token}/sendMessage"
DATA_DIR         = "/app/data"
ATH_PATH         = os.path.join(DATA_DIR, "ath.json")

# ----------------------- Logging -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wallet-monitor")

# ----------------------- HTTP session -----------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent":"Mozilla/5.0 (X11; Linux x86_64)"})

# simple rate-limiting & retry
_last_req_ts = 0.0
REQS_PER_SEC = 5
MIN_GAP = 1.0 / REQS_PER_SEC

def safe_json(r):
    if r is None: return None
    if not getattr(r, "ok", False):
        return None
    try:
        return r.json()
    except Exception:
        return None

def safe_get(url, params=None, timeout=12, retries=3, backoff=1.5):
    global _last_req_ts
    for i in range(retries):
        # rate-limit
        gap = time.time() - _last_req_ts
        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)
        try:
            resp = SESSION.get(url, params=params, timeout=timeout)
            _last_req_ts = time.time()
            if resp.status_code == 200:
                return resp
            if resp.status_code in (404, 429, 502, 503):
                time.sleep(backoff * (i+1))
                continue
            # other codes: break
            return resp
        except Exception:
            time.sleep(backoff * (i+1))
    return None

# ----------------------- Shutdown event -----------------------
shutdown_event = threading.Event()

# ----------------------- State -----------------------
_seen_tx_hashes   = set()
_last_prices      = {}               # pairSlug -> price
_price_history    = {}               # pairSlug -> deque
_last_pair_tx     = {}               # pairSlug -> last tx hash
_tracked_pairs    = set()            # "cronos/0xpair"
_known_pairs_meta = {}               # slug -> dexscreener pair meta

# balances & meta
_token_balances = defaultdict(float) # key: "CRO" or contract (0x..)
_token_meta     = {}                 # key -> {"symbol","decimals"}

# cost basis
_position_qty   = defaultdict(float) # key -> qty
_position_cost  = defaultdict(float) # key -> total cost (USD)

_realized_pnl_today = 0.0
EPSILON = 1e-12

# report cadence
_last_intraday_sent = 0.0

# price cache
PRICE_CACHE = {}
PRICE_CACHE_TTL = 60

# ATHs
ATH = {}  # key (contract or symbol) -> float

# alerts state (cooldowns)
_alert_last_sent = {}  # key -> ts
COOLDOWN_SEC = 60*30   # 30m

# guard state (after buys)
_guard = {}  # key -> {"entry":float,"peak":float,"start_ts":float}

# Ensure data dir & timezone
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass
try:
    os.environ["TZ"] = TZ
except Exception:
    pass

# ----------------------- Utils -----------------------
def now_dt():
    return datetime.now()

def ymd(dt=None):
    if dt is None: dt = now_dt()
    return dt.strftime("%Y-%m-%d")

def month_prefix(dt=None):
    if dt is None: dt = now_dt()
    return dt.strftime("%Y-%m")

def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

def read_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def _format_amount(a):
    try:
        a = float(a)
    except Exception:
        return str(a)
    if abs(a) >= 1: return f"{a:,.4f}"
    if abs(a) >= 0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

def _format_price(p):
    try:
        p = float(p)
    except Exception:
        return str(p)
    return f"{p:,.6f}"

def _nonzero(v, eps=1e-12):
    try: return abs(float(v)) > eps
    except Exception: return False

def send_telegram(message: str) -> bool:
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            log.warning("Telegram not configured.")
            return False
        url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
        r = safe_get(url, params=payload, timeout=12, retries=2)
        if not r or r.status_code != 200:
            if r: log.warning("Telegram status %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.exception("send_telegram exception: %s", e)
        return False

# ----------------------- Telegram commands (getUpdates) -----------------------
_TELEGRAM_UPDATE_OFFSET = 0

def _tg_get_updates(timeout=20):
    """Long-poll Telegram getUpdates with offset."""
    global _TELEGRAM_UPDATE_OFFSET
    if not TELEGRAM_BOT_TOKEN:
        return []
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {
        "timeout": timeout,
        "offset": _TELEGRAM_UPDATE_OFFSET + 1,
        "allowed_updates": json.dumps(["message"])
    }
    r = safe_get(url, params=params, timeout=timeout+5, retries=2)
    data = safe_json(r) or {}
    results = data.get("result") or []
    # advance offset
    for upd in results:
        upd_id = upd.get("update_id")
        if isinstance(upd_id, int) and upd_id > _TELEGRAM_UPDATE_OFFSET:
            _TELEGRAM_UPDATE_OFFSET = upd_id
    return results

def _norm_cmd(text: str) -> str:
    """Normalizes the text to catch '/show wallet assets' variations."""
    if not text:
        return ""
    t = text.strip().lower()
    # unify variants
    if t in ("/show wallet assets", "/show_wallet_assets", "/showwalletassets"):
        return "/show_wallet_assets"
    return t

def _format_wallet_assets_message():
    """
    Δημιουργεί μήνυμα για τα assets σου:
    - Balances ανά token
    - Live price (USD)
    - Value (USD)
    - Σύνολο & Unrealized PnL (open positions)
    """
    total, breakdown, unrealized = compute_holdings_usd()
    if not breakdown:
        return "📦 Δεν βρέθηκαν θετικά balances αυτή τη στιγμή."

    lines = ["*💼 Wallet Assets (MTM):*"]
    # ταξινόμηση με βάση την αξία
    breakdown_sorted = sorted(breakdown, key=lambda b: float(b.get("usd_value",0.0)), reverse=True)
    for b in breakdown_sorted:
        tok = b["token"]
        amt = b["amount"]
        pr  = b["price_usd"] or 0.0
        val = b["usd_value"] or 0.0
        lines.append(f"• *{tok}*: {_format_amount(amt)} @ ${_format_price(pr)} = ${_format_amount(val)}")

    lines.append(f"\n*Σύνολο:* ${_format_amount(total)}")
    if _nonzero(unrealized):
        lines.append(f"*Unrealized PnL (open):* ${_format_amount(unrealized)}")

    # Προσθήκη γρήγορου snapshot από το runtime (μόνο ποσότητες)
    snap = get_wallet_balances_snapshot()
    if snap:
        lines.append("\n_Quantities snapshot:_")
        for sym, amt in sorted(snap.items(), key=lambda x: abs(x[1]), reverse=True):
            lines.append(f"  – {sym}: {_format_amount(amt)}")

    return "\n".join(lines)

# ----------------------- ATH persistence -----------------------
def load_ath():
    global ATH
    ATH = read_json(ATH_PATH, default={})
    if not isinstance(ATH, dict): ATH = {}

def save_ath():
    write_json(ATH_PATH, ATH)

def update_ath(key: str, live_price: float):
    if not _nonzero(live_price): return
    prev = ATH.get(key)
    if prev is None or live_price > prev + 1e-12:
        ATH[key] = live_price
        save_ath()
        send_telegram(f"🏆 New ATH {key}: ${_format_price(live_price)}")

# ----------------------- Price helpers -----------------------
def _pick_best_price(pairs):
    """Pick best USD price by highest liquidity."""
    if not pairs: return None
    best = None
    best_liq = -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0: continue
            if liq > best_liq:
                best_liq = liq
                best = price
        except Exception:
            continue
    return best

def _pairs_for_token_addr(addr: str):
    # try /tokens/chain/addr → /tokens/addr → search
    url1 = f"{DEX_BASE_TOKENS}/cronos/{addr}"
    r = safe_get(url1, timeout=10)
    data = safe_json(r) or {}
    pairs = data.get("pairs") or []
    if not pairs:
        url2 = f"{DEX_BASE_TOKENS}/{addr}"
        r = safe_get(url2, timeout=10)
        data = safe_json(r) or {}
        pairs = data.get("pairs") or []
    if not pairs:
        r = safe_get(DEX_BASE_SEARCH, params={"q": addr}, timeout=10)
        data = safe_json(r) or {}
        pairs = data.get("pairs") or []
    return pairs

def get_price_usd(symbol_or_addr: str):
    """Generic price by symbol or contract; cached."""
    if not symbol_or_addr: return None
    key = symbol_or_addr.strip().lower()
    now_ts = time.time()
    cached = PRICE_CACHE.get(key)
    if cached and (now_ts - cached[1] < PRICE_CACHE_TTL):
        return cached[0]

    price = None
    try:
        if key in ("cro","wcro","w-cro","wrappedcro","wrapped cro"):
            # search CRO/WCRO vs USDT
            r = safe_get(DEX_BASE_SEARCH, params={"q":"wcro usdt"}, timeout=10)
            data = safe_json(r) or {}
            price = _pick_best_price(data.get("pairs"))
            if not price:
                r = safe_get(DEX_BASE_SEARCH, params={"q":"cro usdt"}, timeout=10)
                data = safe_json(r) or {}
                price = _pick_best_price(data.get("pairs"))
        elif key.startswith("0x") and len(key)==42:
            price = _pick_best_price(_pairs_for_token_addr(key))
        else:
            # symbol search and symbol+usdt
            r = safe_get(DEX_BASE_SEARCH, params={"q":key}, timeout=10)
            data = safe_json(r) or {}
            price = _pick_best_price(data.get("pairs"))
            if not price and len(key)<=8:
                r = safe_get(DEX_BASE_SEARCH, params={"q":f"{key} usdt"}, timeout=10)
                data = safe_json(r) or {}
                price = _pick_best_price(data.get("pairs"))
    except Exception:
        price = None

    PRICE_CACHE[key] = (price, now_ts)
    return price

def get_change_and_price_for_symbol_or_addr(sym_or_addr: str):
    """
    Return tuple (priceUsd, change24h, change2h) using best Cronos pair found.
    """
    pairs = []
    if sym_or_addr.lower().startswith("0x") and len(sym_or_addr)==42:
        pairs = _pairs_for_token_addr(sym_or_addr)
    else:
        r = safe_get(DEX_BASE_SEARCH, params={"q": sym_or_addr}, timeout=10)
        data = safe_json(r) or {}
        pairs = data.get("pairs") or []
    # pick best
    best = None
    best_liq = -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0: continue
            if liq > best_liq:
                best_liq = liq
                best = p
        except Exception:
            continue
    if not best:
        return (None, None, None, None)  # price, ch24, ch2h, ds_url
    price = float(best.get("priceUsd") or 0)
    ch24 = None
    ch2h = None
    try:
        ch = best.get("priceChange") or {}
        if "h24" in ch: ch24 = float(ch.get("h24"))
        if "h2"  in ch: ch2h  = float(ch.get("h2"))
    except Exception:
        pass
    ds_url = f"https://dexscreener.com/cronos/{best.get('pairAddress')}"
    return (price, ch24, ch2h, ds_url)

# ----------------------- Etherscan fetchers -----------------------
def fetch_latest_wallet_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params = {
        "chainid": CRONOS_CHAINID,
        "module":"account","action":"txlist",
        "address": WALLET_ADDRESS,
        "startblock":0,"endblock":99999999,
        "page":1,"offset":limit,"sort":"desc",
        "apikey": ETHERSCAN_API
    }
    r = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)
    data = safe_json(r) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

def fetch_latest_token_txs(limit=50):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params = {
        "chainid": CRONOS_CHAINID,
        "module":"account","action":"tokentx",
        "address": WALLET_ADDRESS,
        "startblock":0,"endblock":99999999,
        "page":1,"offset":limit,"sort":"desc",
        "apikey": ETHERSCAN_API
    }
    r = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)
    data = safe_json(r) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list):
        return data["result"]
    return []

# ----------------------- Ledger helpers -----------------------
def _append_ledger(entry: dict):
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    data["entries"].append(entry)
    data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
    data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0))
    write_json(path, data)

def _replay_today_cost_basis():
    global _position_qty, _position_cost, _realized_pnl_today
    _position_qty.clear(); _position_cost.clear(); _realized_pnl_today = 0.0
    data = read_json(data_file_for_today(), default=None)
    if not isinstance(data, dict): return
    for e in data.get("entries", []):
        key = (e.get("token_addr") or (e.get("token") if e.get("token")=="CRO" else None)) or "CRO"
        amt = float(e.get("amount") or 0.0)
        price = float(e.get("price_usd") or 0.0)
        realized = _update_cost_basis(key, amt, price)
        e["realized_pnl"] = realized
    try:
        total_real = sum(float(e.get("realized_pnl", 0.0)) for e in data.get("entries", []))
        data["realized_pnl"] = total_real
        write_json(data_file_for_today(), data)
    except Exception:
        pass

# ----------------------- Cost-basis / PnL -----------------------
def _update_cost_basis(token_key: str, signed_amount: float, price_usd: float):
    global _realized_pnl_today
    qty = _position_qty[token_key]
    cost= _position_cost[token_key]
    realized = 0.0
    if signed_amount > EPSILON:
        buy_qty = signed_amount
        _position_qty[token_key] = qty + buy_qty
        _position_cost[token_key] = cost + buy_qty * (price_usd or 0.0)
    elif signed_amount < -EPSILON:
        sell_qty_req = -signed_amount
        if qty > EPSILON:
            sell_qty = min(sell_qty_req, qty)
            avg_cost = (cost/qty) if qty > EPSILON else (price_usd or 0.0)
            realized = (price_usd - avg_cost) * sell_qty
            _position_qty[token_key]  = qty - sell_qty
            _position_cost[token_key] = max(0.0, cost - avg_cost * sell_qty)
        else:
            realized = 0.0
    _realized_pnl_today += realized
    return realized

# ----------------------- Handlers -----------------------
def handle_native_tx(tx: dict):
    h = tx.get("hash")
    if not h or h in _seen_tx_hashes: return
    _seen_tx_hashes.add(h)

    val_raw = tx.get("value","0")
    try: amount_cro = int(val_raw)/10**18
    except Exception:
        try: amount_cro = float(val_raw)
        except Exception: amount_cro = 0.0

    frm = (tx.get("from") or "").lower()
    to  = (tx.get("to") or "").lower()
    ts  = int(tx.get("timeStamp") or 0)
    dt  = datetime.fromtimestamp(ts) if ts>0 else now_dt()

    sign = +1 if to==WALLET_ADDRESS else (-1 if frm==WALLET_ADDRESS else 0)
    if sign==0 or abs(amount_cro)<=EPSILON: return

    price = get_price_usd("CRO") or 0.0
    usd_value = sign * amount_cro * price

    _token_balances["CRO"] += sign * amount_cro
    _token_meta["CRO"] = {"symbol":"CRO","decimals":18}

    realized = _update_cost_basis("CRO", sign*amount_cro, price)

    link = CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\n"
        f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount_cro:.6f} CRO\n"
        f"Price: ${_format_price(price)}\n"
        f"USD value: ${_format_amount(usd_value)}"
    )

    entry = {
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h, "type":"native",
        "token":"CRO","token_addr":None,
        "amount": sign*amount_cro,
        "price_usd": price,
        "usd_value": usd_value,
        "realized_pnl": realized,
        "from": frm, "to": to,
    }
    _append_ledger(entry)

def _mini_summary_line(token_key, symbol_shown):
    # open qty & avg & unreal (live)
    open_qty  = _position_qty.get(token_key,0.0)
    open_cost = _position_cost.get(token_key,0.0)
    live = None
    if token_key=="CRO":
        live = get_price_usd("CRO") or 0.0
    elif isinstance(token_key,str) and token_key.startswith("0x"):
        live = get_price_usd(token_key) or 0.0
    else:
        live = get_price_usd(symbol_shown) or 0.0
    unreal = 0.0
    if open_qty>EPSILON and _nonzero(live):
        unreal = open_qty*live - open_cost
    send_telegram(
        f"• {'Open' if open_qty>0 else 'Flat'} {symbol_shown} "
        f"{_format_amount(open_qty)} @ live ${_format_price(live)}\n"
        f"   Avg: ${_format_price((open_cost/open_qty) if open_qty>EPSILON else 0)} | "
        f"Unreal: ${_format_amount(unreal)}"
    )

def handle_erc20_tx(t: dict):
    h = t.get("hash")
    if not h: return
    frm = (t.get("from") or "").lower()
    to  = (t.get("to") or "").lower()
    if WALLET_ADDRESS not in (frm,to): return

    token_addr = (t.get("contractAddress") or "").lower()
    symbol = t.get("tokenSymbol") or (token_addr[:8] if token_addr else "?")
    try: decimals = int(t.get("tokenDecimal") or 18)
    except Exception: decimals = 18

    val_raw = t.get("value","0")
    try: amount = int(val_raw)/(10**decimals)
    except Exception:
        try: amount = float(val_raw)
        except Exception: amount = 0.0

    ts = int(t.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(ts) if ts>0 else now_dt()
    sign = +1 if to==WALLET_ADDRESS else -1

    # prefer contract-based price
    if token_addr and token_addr.startswith("0x") and len(token_addr)==42:
        price = get_price_usd(token_addr) or 0.0
    else:
        price = get_price_usd(symbol) or 0.0
    usd_value = sign * amount * (price or 0.0)

    # update balances & meta
    key = token_addr if token_addr else symbol
    _token_balances[key] += sign * amount
    # clamp near zero to zero
    if abs(_token_balances[key]) < 1e-10: _token_balances[key] = 0.0
    _token_meta[key] = {"symbol": symbol, "decimals": decimals}

    realized = _update_cost_basis(key, sign*amount, (price or 0.0))

    # ATH update
    try:
        if _nonzero(price):
            ath_key = token_addr if token_addr else symbol
            update_ath(ath_key, price)
    except Exception:
        pass

    # send TX line
    link = CRONOS_TX.format(txhash=h)
    direction = "IN" if sign>0 else "OUT"
    send_telegram(
        f"Token TX ({direction}) {symbol}\n"
        f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {symbol}\n"
        f"Price: ${_format_price(price)}\n"
        f"USD value: ${_format_amount(usd_value)}"
    )

    # mini summary (open/avg/unreal)
    if sign>0:
        send_telegram(f"• BUY {symbol} {_format_amount(amount)} @ live ${_format_price(price)}")
    else:
        send_telegram(f"• SELL {symbol} {_format_amount(-amount)} @ live ${_format_price(price)}")
    _mini_summary_line(key, symbol)

    # guard logic for buys
    if sign>0 and _nonzero(price):
        _guard[key] = {"entry": float(price), "peak": float(price), "start_ts": time.time()}

    entry = {
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h, "type":"erc20",
        "token": symbol, "token_addr": token_addr or None,
        "amount": sign*amount,
        "price_usd": price or 0.0,
        "usd_value": usd_value,
        "realized_pnl": realized,
        "from": frm, "to": to,
    }
    _append_ledger(entry)

# ----------------------- Wallet monitor loop -----------------------

def wallet_monitor_loop():
    log.info("Wallet monitor starting; loading initial recent txs...")
    initial = fetch_latest_wallet_txs(limit=50)
    try:
        for tx in initial:
            h = tx.get("hash")
            if h: _seen_tx_hashes.add(h)
    except Exception:
        pass

    _replay_today_cost_basis()
    send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}` (Cronos).")

    last_tokentx_seen = set()
    while not shutdown_event.is_set():
        # native
        try:
            txs = fetch_latest_wallet_txs(limit=25)
            for tx in reversed(txs):
                if not isinstance(tx, dict): continue
                h = tx.get("hash")
                if h in _seen_tx_hashes: continue
                handle_native_tx(tx)
        except Exception as e:
            log.exception("wallet native loop error: %s", e)

        # erc20
        try:
            toks = fetch_latest_token_txs(limit=60)
            for t in reversed(toks):
                h = t.get("hash")
                if h and h in last_tokentx_seen: continue
                handle_erc20_tx(t)
                if h: last_tokentx_seen.add(h)
            if len(last_tokentx_seen) > 600:
                last_tokentx_seen = set(list(last_tokentx_seen)[-400:])
        except Exception as e:
            log.exception("wallet token loop error: %s", e)

        for _ in range(WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Dexscreener pair monitor + discovery -----------------------
def slug(chain: str, pair_address: str) -> str:
    return f"{chain}/{pair_address}".lower()

def fetch_pair(slug_str: str):
    url = f"{DEX_BASE_PAIRS}/{slug_str}"
    r = safe_get(url, timeout=12)
    return safe_json(r)

def fetch_token_pairs(chain: str, token_address: str):
    url = f"{DEX_BASE_TOKENS}/{chain}/{token_address}"
    r = safe_get(url, timeout=12)
    data = safe_json(r) or {}
    pairs = data.get("pairs") or []
    return pairs

def fetch_search(query: str):
    r = safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=15)
    data = safe_json(r) or {}
    return data.get("pairs") or []

def ensure_tracking_pair(chain: str, pair_address: str, meta: dict=None):
    s = slug(chain, pair_address)
    if s in _tracked_pairs: return
    _tracked_pairs.add(s)
    _last_prices[s]   = None
    _last_pair_tx[s]  = None
    _price_history[s] = deque(maxlen=PRICE_WINDOW)
    if meta: _known_pairs_meta[s] = meta
    ds_link = f"https://dexscreener.com/{chain}/{pair_address}"
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
    if not hist or len(hist) < 2: return None
    first = hist[0]; last = hist[-1]
    if not first: return None
    pct = (last-first)/first*100.0
    return pct if abs(pct) >= SPIKE_THRESHOLD else None

def _pair_passes_filters(p):
    try:
        if str(p.get("chainId","")).lower() != "cronos": return False
        bt = p.get("baseToken") or {}
        qt = p.get("quoteToken") or {}
        base_sym = (bt.get("symbol") or "").upper()
        quote_sym= (qt.get("symbol") or "").upper()
        if DISCOVER_REQUIRE_WCRO and quote_sym != "WCRO": return False
        if DISCOVER_BASE_WHITELIST and base_sym not in DISCOVER_BASE_WHITELIST: return False
        if DISCOVER_BASE_BLACKLIST and base_sym in DISCOVER_BASE_BLACKLIST: return False
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq < DISCOVER_MIN_LIQ_USD: return False
        vol24 = float((p.get("volume") or {}).get("h24") or 0)
        if vol24 < DISCOVER_MIN_VOL24_USD: return False
        ch = p.get("priceChange") or {}
        best_change = 0.0
        for k in ("h1","h4","h6","h24"):
            if k in ch:
                try:
                    best_change = max(best_change, abs(float(ch[k])))
                except Exception: pass
        if best_change < DISCOVER_MIN_ABS_CHANGE_PCT: return False
        created_ms = p.get("pairCreatedAt")
        if created_ms:
            age_h = (time.time()*1000 - float(created_ms))/1000/3600.0
            if age_h > DISCOVER_MAX_PAIR_AGE_HOURS: return False
        return True
    except Exception:
        return False

def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        log.info("No tracked pairs; monitor waits until discovery/seed adds some.")
    else:
        send_telegram(f"🚀 Dex monitor started: {', '.join(sorted(_tracked_pairs))}")

    while not shutdown_event.is_set():
        if not _tracked_pairs:
            time.sleep(DEX_POLL); continue
        for s in list(_tracked_pairs):
            try:
                data = fetch_pair(s)
                if not data: continue
                pair = None
                if isinstance(data.get("pair"), dict):
                    pair = data["pair"]
                elif isinstance(data.get("pairs"), list) and data["pairs"]:
                    pair = data["pairs"][0]
                if not pair: continue
                try:
                    price_val = float(pair.get("priceUsd") or 0)
                except Exception:
                    price_val = None
                if price_val and price_val>0:
                    update_price_history(s, price_val)
                    spike_pct = detect_spike(s)
                    if spike_pct is not None:
                        vol_h1 = None
                        try:
                            vol_h1 = float((pair.get("volume") or {}).get("h1") or 0)
                        except Exception:
                            vol_h1 = None
                        if not (MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1 < MIN_VOLUME_FOR_ALERT):
                            bt = pair.get("baseToken") or {}
                            symbol = bt.get("symbol") or s
                            send_telegram(f"🚨 Spike on {symbol}: {spike_pct:.2f}%\nPrice: ${price_val:.6f}")
                            _price_history[s].clear()
                            _last_prices[s] = price_val
                prev = _last_prices.get(s)
                if prev and price_val and prev>0:
                    delta = (price_val-prev)/prev*100.0
                    if abs(delta) >= PRICE_MOVE_THRESHOLD:
                        bt = pair.get("baseToken") or {}
                        symbol = bt.get("symbol") or s
                        send_telegram(f"📈 Price move on {symbol}: {delta:.2f}%\nPrice: ${price_val:.6f} (prev ${prev:.6f})")
                        _last_prices[s] = price_val
                last_tx = (pair.get("lastTx") or {}).get("hash")
                if last_tx:
                    prev_tx = _last_pair_tx.get(s)
                    if prev_tx != last_tx:
                        _last_pair_tx[s] = last_tx
                        bt = pair.get("baseToken") or {}
                        symbol = bt.get("symbol") or s
                        send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx)}")
            except Exception as e:
                log.debug("pairs loop error %s: %s", s, e)
        for _ in range(DEX_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

def discovery_loop():
    # seed from DEX_PAIRS
    seeds = [p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    for s in seeds:
        if s.startswith("cronos/"):
            ensure_tracking_pair("cronos", s.split("/",1)[1])

    # seed from TOKENS -> top pair
    token_items = [t.strip().lower() for t in (TOKENS or "").split(",") if t.strip()]
    for t in token_items:
        if not t.startswith("cronos/"): continue
        _, token_addr = t.split("/",1)
        pairs = fetch_token_pairs("cronos", token_addr)
        if pairs:
            p = pairs[0]
            pair_addr = p.get("pairAddress")
            if pair_addr:
                ensure_tracking_pair("cronos", pair_addr, meta=p)

    if not DISCOVER_ENABLED:
        log.info("Discovery disabled."); return

    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos) with filters.")
    while not shutdown_event.is_set():
        try:
            found = fetch_search(DISCOVER_QUERY)
            adopted = 0
            for p in found or []:
                if not _pair_passes_filters(p): continue
                pair_addr = p.get("pairAddress"); 
                if not pair_addr: continue
                s = slug("cronos", pair_addr)
                if s in _tracked_pairs: continue
                ensure_tracking_pair("cronos", pair_addr, meta=p)
                adopted += 1
                if adopted >= DISCOVER_LIMIT: break
        except Exception as e:
            log.debug("Discovery error: %s", e)
        for _ in range(DISCOVER_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Wallet snapshot (runtime only) -----------------------
def get_wallet_balances_snapshot():
    """
    Χωρίς Cronoscan: χρησιμοποιούμε ΜΟΝΟ τα runtime balances (_token_balances)
    και αγνοούμε μηδενικά/αρνητικά. Επιστρέφει dict {symbol: amount}.
    """
    balances = {}
    # CRO
    cro_amt = float(_token_balances.get("CRO",0.0))
    if cro_amt > EPSILON:
        balances["CRO"] = balances.get("CRO",0.0) + cro_amt
    # ERC20
    for k, v in list(_token_balances.items()):
        if k=="CRO": continue
        amt = float(v)
        if amt <= EPSILON: continue
        meta = _token_meta.get(k,{})
        sym = meta.get("symbol") or (k[:8] if isinstance(k,str) else "?")
        balances[sym] = balances.get(sym,0.0) + amt
    return balances

# ----------------------- Compute holdings / MTM -----------------------
def compute_holdings_usd():
    """
    Υπολογίζει MTM για ΟΛΑ τα assets με τρέχον θετικό balance.
    Επιστρέφει: total, breakdown(list of {token,token_addr,amount,price_usd,usd_value}), unrealized_sum
    Unrealized: μόνο για ανοιχτές θέσεις (open qty>0) και price>0, με cost-basis.
    """
    total = 0.0
    breakdown = []
    unrealized = 0.0

    # CRO
    cro_amt = max(0.0, _token_balances.get("CRO",0.0))
    if cro_amt > EPSILON:
        cro_price = get_price_usd("CRO") or 0.0
        cro_val = cro_amt * cro_price
        total += cro_val
        breakdown.append({"token":"CRO","token_addr":None,"amount":cro_amt,"price_usd":cro_price,"usd_value":cro_val})
        rem_qty = _position_qty.get("CRO",0.0)
        rem_cost= _position_cost.get("CRO",0.0)
        if rem_qty > EPSILON and _nonzero(cro_price):
            unrealized += (cro_amt*cro_price - rem_cost)

    # Tokens
    for addr, amt in list(_token_balances.items()):
        if addr=="CRO": continue
        amt = max(0.0, float(amt))
        if amt <= EPSILON: continue
        meta = _token_meta.get(addr,{})
        sym = meta.get("symbol") or (addr[:8] if isinstance(addr,str) else "?")
        if isinstance(addr,str) and addr.startswith("0x") and len(addr)==42:
            price = get_price_usd(addr) or 0.0
        else:
            price = get_price_usd(sym) or 0.0
        val = amt * (price or 0.0)
        total += val
        breakdown.append({"token":sym,"token_addr":addr,"amount":amt,"price_usd":price or 0.0,"usd_value":val})
        rem_qty = _position_qty.get(addr,0.0)
        rem_cost= _position_cost.get(addr,0.0)
        if rem_qty > EPSILON and _nonzero(price):
            unrealized += (amt*price - rem_cost)
    return total, breakdown, unrealized

# ----------------------- Month aggregates -----------------------
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

# ----------------------- Per-asset summarize (today, clean) -----------------------
def summarize_today_per_asset():
    """
    Contract-first aggregation:
      - Group by token_addr if υπάρχει, αλλιώς by token symbol.
      - Net flow, realized PnL, today net qty (μπορεί να είναι αρνητικό).
      - Live price για unrealized ΜΟΝΟ αν currently open qty > 0 (global state).
    """
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])

    agg = {}  # key -> dict
    for e in entries:
        addr = e.get("token_addr") or ""
        sym  = e.get("token") or "?"
        key  = addr if (addr and addr.startswith("0x")) else sym
        rec = agg.get(key)
        if not rec:
            rec = {
                "token_addr": addr if addr else None,
                "symbol": sym,
                "net_flow_today": 0.0,
                "realized_today": 0.0,
                "net_qty_today": 0.0,
                "last_price_seen": 0.0
            }
            agg[key] = rec
        rec["net_flow_today"] += float(e.get("usd_value") or 0.0)
        rec["realized_today"] += float(e.get("realized_pnl") or 0.0)
        rec["net_qty_today"]  += float(e.get("amount") or 0.0)
        p = float(e.get("price_usd") or 0.0)
        if p>0: rec["last_price_seen"] = p

    # enrich with live price / unreal only for open qty now
    result = []
    for key, rec in agg.items():
        addr = rec["token_addr"]
        sym  = rec["symbol"]
        # open qty now (global state)
        gkey = addr if addr else (sym if sym=="CRO" else addr or sym)
        open_qty_now = _position_qty.get(gkey, 0.0)
        # live price (prefer contract)
        price_now = None
        if addr and addr.startswith("0x"):
            price_now = get_price_usd(addr) or None
        else:
            price_now = get_price_usd(sym) or None
        if price_now is None or price_now==0:
            if rec["last_price_seen"]>0:
                price_now = rec["last_price_seen"]
            else:
                price_now = 0.0
        unreal = 0.0
        if open_qty_now > EPSILON and _nonzero(price_now):
            cost = _position_cost.get(gkey, 0.0)
            # value now for open qty (not today's net)
            # (αν θέλουμε ακριβές per-asset unreal: value_now - cost)
            unreal = open_qty_now*price_now - cost
        result.append({
            "symbol": sym,
            "token_addr": addr,
            "net_flow_today": rec["net_flow_today"],
            "realized_today": rec["realized_today"],
            "net_qty_today": rec["net_qty_today"],
            "price_now": price_now or 0.0,
            "unreal_now": unreal
        })
    # order by abs(net_flow_today)
    result.sort(key=lambda r: abs(r["net_flow_today"]), reverse=True)
    return result

# ----------------------- Report builder -----------------------
def build_day_report_text():
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])
    net_flow = float(data.get("net_usd_flow", 0.0))
    realized_today = float(data.get("realized_pnl", 0.0))

    lines = [f"*📒 Daily Report* ({data.get('date')})"]
    if not entries:
        lines.append("_No transactions today._")
    else:
        lines.append("*Transactions:*")
        MAX_LINES = 20
        for e in entries[-MAX_LINES:]:
            tok = e.get("token") or "?"
            amt = e.get("amount") or 0
            usd = e.get("usd_value") or 0
            tm  = (e.get("time","")[-8:]) or ""
            direction = "IN" if float(amt)>0 else "OUT"
            unit_price = e.get("price_usd") or 0.0
            pnl_line = ""
            rp = float(e.get("realized_pnl",0.0) or 0.0)
            if abs(rp) > 1e-9: pnl_line = f"  PnL: ${_format_amount(rp)}"
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
            tok = b["token"]; amt=b["amount"]; pr=b["price_usd"]; val=b["usd_value"]
            lines.append(f"  – {tok}: {_format_amount(amt)} @ ${_format_price(pr)} = ${_format_amount(val)}")
        if len(breakdown)>15:
            lines.append(f"  …and {len(breakdown)-15} more.")
    lines.append(f"*Unrealized PnL (open positions):* ${_format_amount(unrealized)}")

    # per-asset today
    per = summarize_today_per_asset()
    if per:
        lines.append("\n*Per-Asset Summary (Today):*")
        LIMIT = 12
        for rec in per[:LIMIT]:
            tok = rec["symbol"]; flow=rec["net_flow_today"]; real=rec["realized_today"]
            qty = rec["net_qty_today"]; pr=rec["price_now"]; un=rec["unreal_now"]
            base = f"  • {tok}: flow ${_format_amount(flow)} | realized ${_format_amount(real)} | today qty {_format_amount(qty)} | price ${_format_price(pr)}"
            if _nonzero(un):
                base += f" | unreal ${_format_amount(un)}"
            lines.append(base)
        if len(per)>LIMIT:
            lines.append(f"  …and {len(per)-LIMIT} more.")

    month_flow, month_real = sum_month_net_flows_and_realized()
    lines.append(f"\n*Month Net Flow:* ${_format_amount(month_flow)}")
    lines.append(f"*Month Realized PnL:* ${_format_amount(month_real)}")
    return "\n".join(lines)

# ----------------------- Intraday & EOD reporters -----------------------
def intraday_report_loop():
    global _last_intraday_sent
    time.sleep(5)
    send_telegram("⏱ Intraday reporting enabled.")
    while not shutdown_event.is_set():
        try:
            if time.time() - _last_intraday_sent >= INTRADAY_HOURS*3600:
                txt = build_day_report_text()
                send_telegram("🟡 *Intraday Update*\n" + txt)
                _last_intraday_sent = time.time()
        except Exception as e:
            log.exception("Intraday error: %s", e)
        for _ in range(30):
            if shutdown_event.is_set(): break
            time.sleep(1)

def end_of_day_scheduler_loop():
    send_telegram(f"🕛 End-of-day scheduler active (at {EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ}).")
    while not shutdown_event.is_set():
        now = now_dt()
        target = now.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)
        if now > target: target = target + timedelta(days=1)
        wait_s = (target - now).total_seconds()
        while wait_s > 0 and not shutdown_event.is_set():
            s = min(wait_s, 30)
            time.sleep(s); wait_s -= s
        if shutdown_event.is_set(): break
        try:
            txt = build_day_report_text()
            send_telegram("🟢 *End of Day Report*\n" + txt)
        except Exception as e:
            log.exception("EOD error: %s", e)

# ----------------------- Reconciliation helper (basic pairing) -----------------------
def reconcile_swaps_from_entries():
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = data.get("entries", [])
    swaps = []
    i = 0
    while i < len(entries):
        e = entries[i]
        if float(e.get("amount",0.0)) < 0:
            j = i+1
            while j < len(entries) and j <= i+6:
                e2 = entries[j]
                if float(e2.get("amount",0.0)) > 0 and e2.get("token") != e.get("token"):
                    swaps.append((e, e2))
                    break
                j += 1
        i += 1
    return swaps

# ----------------------- Alerts Monitor (wallet 24h & risky recent) -----------------------
def _cooldown_ok(key):
    last = _alert_last_sent.get(key, 0.0)
    if time.time() - last >= COOLDOWN_SEC:
        _alert_last_sent[key] = time.time()
        return True
    return False

def alerts_monitor_loop():
    send_telegram(f"🛰 Alerts monitor active every {ALERTS_INTERVAL_MIN}m. Wallet 24h dump/pump: {DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT}.")
    while not shutdown_event.is_set():
        try:
            # Wallet coins (non-zero balances)
            wallet_bal = get_wallet_balances_snapshot()  # {symbol: amount}
            for sym, amt in list(wallet_bal.items()):
                if amt <= EPSILON: continue
                price, ch24, ch2h, url = get_change_and_price_for_symbol_or_addr(sym)
                if not price or price<=0: continue  # skip dead
                # 24h dump/pump
                if ch24 is not None:
                    key_p = f"24h_pump:{sym}"
                    key_d = f"24h_dump:{sym}"
                    if ch24 >= PUMP_ALERT_24H_PCT and _cooldown_ok(key_p):
                        send_telegram(f"🚀 Pump Alert {sym} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{url}")
                    if ch24 <= DUMP_ALERT_24H_PCT and _cooldown_ok(key_d):
                        send_telegram(f"⚠️ Dump Alert {sym} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{url}")

            # Risky = today's recent buys (no env list constraint -> auto)
            data = read_json(data_file_for_today(), default={"entries":[]})
            seen = set()
            for e in data.get("entries", []):
                if float(e.get("amount") or 0) > 0:
                    sym = e.get("token") or "?"
                    addr= (e.get("token_addr") or "").lower()
                    key = addr if (addr and addr.startswith("0x")) else sym
                    if key in seen: continue
                    seen.add(key)
                    # prefer contract for accuracy
                    query = addr if (addr and addr.startswith("0x")) else sym
                    price, ch24, ch2h, url = get_change_and_price_for_symbol_or_addr(query)
                    if not price or price<=0: continue
                    # try 2h first for recent buys
                    ch = ch2h if (ch2h is not None) else ch24
                    if ch is None: continue
                    # thresholds: reuse wallet 24h defaults (simple)
                    key_base = f"risky:{key}"
                    if ch >= PUMP_ALERT_24H_PCT and _cooldown_ok(key_base+":pump"):
                        send_telegram(f"🚀 Pump (recent) {sym} {ch:.2f}%\nPrice ${_format_price(price)}\n{url}")
                    if ch <= DUMP_ALERT_24H_PCT and _cooldown_ok(key_base+":dump"):
                        send_telegram(f"⚠️ Dump (recent) {sym} {ch:.2f}%\nPrice ${_format_price(price)}\n{url}")

        except Exception as e:
            log.exception("alerts monitor error: %s", e)

        for _ in range(ALERTS_INTERVAL_MIN*60):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Guard monitor (short-term after buy) -----------------------
def guard_monitor_loop():
    send_telegram(f"🛡 Guard monitor active: {GUARD_WINDOW_MIN}m window, alert below {GUARD_DROP_PCT}% / above {GUARD_PUMP_PCT}% / trailing {GUARD_TRAIL_DROP_PCT}%.")
    while not shutdown_event.is_set():
        try:
            dead_keys = []
            for key, st in list(_guard.items()):
                if time.time() - st["start_ts"] > GUARD_WINDOW_MIN*60:
                    dead_keys.append(key); continue
                # live price
                if key=="CRO":
                    price = get_price_usd("CRO") or 0.0
                elif key.startswith("0x"):
                    price = get_price_usd(key) or 0.0
                else:
                    meta = _token_meta.get(key,{})
                    sym = meta.get("symbol") or key
                    price = get_price_usd(sym) or 0.0
                if not price or price<=0: continue
                entry = st["entry"]
                peak  = st["peak"]
                # update peak
                if price > peak: st["peak"] = price; peak = price
                # compute % from entry
                pct_from_entry = (price-entry)/entry*100.0 if entry>0 else 0.0
                trail_from_peak = (price-peak)/peak*100.0 if peak>0 else 0.0
                sym = _token_meta.get(key,{}).get("symbol") or ("CRO" if key=="CRO" else key[:6])
                # alerts
                if pct_from_entry >= GUARD_PUMP_PCT and _cooldown_ok(f"guard:pump:{key}"):
                    send_telegram(f"🟢 GUARD Pump {sym} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if pct_from_entry <= GUARD_DROP_PCT and _cooldown_ok(f"guard:drop:{key}"):
                    send_telegram(f"🔻 GUARD Dump {sym} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if trail_from_peak <= GUARD_TRAIL_DROP_PCT and _cooldown_ok(f"guard:trail:{key}"):
                    send_telegram(f"🟠 GUARD Trailing {sym} {trail_from_peak:.2f}% from peak ${_format_price(peak)} → ${_format_price(price)}")
            for k in dead_keys:
                _guard.pop(k, None)
        except Exception as e:
            log.exception("guard monitor error: %s", e)
        for _ in range(15):
            if shutdown_event.is_set(): break
            time.sleep(2)

def telegram_commands_loop():
    """
    Απλός long-poll command listener για το Telegram.
    Δέχεται: '/show wallet assets' (ή '/show_wallet_assets' / '/showwalletassets')
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info("Telegram commands disabled (no token/chat id).")
        return

    send_telegram("🧩 Telegram commands listener ενεργό. Δώσε */show wallet assets* για λίστα assets.")

    my_chat_id = str(TELEGRAM_CHAT_ID).strip()

    while not shutdown_event.is_set():
        try:
            updates = _tg_get_updates(timeout=20)
            for upd in updates:
                msg = (upd.get("message") or {})
                chat = (msg.get("chat") or {})
                chat_id = str(chat.get("id") or "")
                if not chat_id or chat_id != my_chat_id:
                    # αγνόησε μηνύματα από άλλα chats
                    continue

                text = msg.get("text") or ""
                cmd = _norm_cmd(text)

                if cmd == "/show_wallet_assets":
                    reply = _format_wallet_assets_message()
                    send_telegram(reply)
                # (εδώ μπορείς να προσθέσεις και άλλα commands στο μέλλον)
        except Exception as e:
            log.exception("telegram_commands_loop error: %s", e)
            # μικρή παύση πριν retry
            for _ in range(3):
                if shutdown_event.is_set(): break
                time.sleep(1)

# ----------------------- Thread runner w/ restart -----------------------
def run_with_restart(fn, name, daemon=True):
    def runner():
        while not shutdown_event.is_set():
            try:
                log.info("Thread %s starting.", name)
                fn()
                log.info("Thread %s exited cleanly.", name)
                break
            except Exception as e:
                log.exception("Thread %s crashed: %s. Restarting in 3s...", name, e)
                for _ in range(3):
                    if shutdown_event.is_set(): break
                    time.sleep(1)
        log.info("Thread %s terminating.", name)
    t = threading.Thread(target=runner, daemon=daemon, name=name)
    t.start()
    return t

# ----------------------- Entrypoint -----------------------
def main():
    load_ath()
    log.info("Starting monitor with config:")
    log.info("WALLET_ADDRESS: %s", WALLET_ADDRESS)
    log.info("TELEGRAM_BOT_TOKEN present: %s", bool(TELEGRAM_BOT_TOKEN))
    log.info("TELEGRAM_CHAT_ID: %s", TELEGRAM_CHAT_ID)
    log.info("ETHERSCAN_API present: %s", bool(ETHERSCAN_API))
    log.info("DEX_PAIRS: %s", DEX_PAIRS)
    log.info("DISCOVER_ENABLED: %s | DISCOVER_QUERY: %s", DISCOVER_ENABLED, DISCOVER_QUERY)
    log.info("TZ: %s | INTRADAY_HOURS: %s | EOD: %02d:%02d", TZ, INTRADAY_HOURS, EOD_HOUR, EOD_MINUTE)
    log.info("Alerts interval: %sm | Wallet 24h dump/pump: %s/%s", ALERTS_INTERVAL_MIN, DUMP_ALERT_24H_PCT, PUMP_ALERT_24H_PCT)

    threads = []
    threads.append(run_with_restart(wallet_monitor_loop, "wallet_monitor"))
    threads.append(run_with_restart(monitor_tracked_pairs_loop, "pairs_monitor"))
    threads.append(run_with_restart(discovery_loop, "discovery"))
    threads.append(run_with_restart(intraday_report_loop, "intraday_report"))
    threads.append(run_with_restart(end_of_day_scheduler_loop, "eod_scheduler"))
    threads.append(run_with_restart(alerts_monitor_loop, "alerts_monitor"))
    threads.append(run_with_restart(guard_monitor_loop, "guard_monitor"))
    threads.append(run_with_restart(telegram_commands_loop, "telegram_commands"))

    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt: shutting down...")
        shutdown_event.set()

    log.info("Waiting for threads to terminate...")
    for t in threading.enumerate():
        if t is threading.current_thread(): continue
        t.join(timeout=2)
    log.info("Shutdown complete.")

# ----------------------- Signal handler -----------------------
def _signal_handler(sig, frame):
    log.info("Signal %s received, initiating shutdown...", sig)
    shutdown_event.set()

signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)

if __name__ == "__main__":
    main()
