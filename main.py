#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Complete Wallet monitor (Cronos via Etherscan Multichain) + Dexscreener Live Scanner
Auto-discovery, PnL (realized & unrealized), intraday/EOD reports, ATH tracking, swap reconciliation,
Mini-summary per trade, Guard after buy (+20% / -12% / trailing -8%), 24h alerts every 15' for all wallet assets.

Drop-in for Railway worker. Uses environment variables (no hardcoded secrets).
"""

import os, sys, time, json, signal, threading, logging
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone
import requests
from dotenv import load_dotenv

load_dotenv()

# ----------------------- Config / ENV -----------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API")
CRONOSCAN_API      = os.getenv("CRONOSCAN_API")  # optional
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")
TOKENS             = os.getenv("TOKENS", "")

PRICE_MOVE_THRESHOLD   = float(os.getenv("PRICE_MOVE_THRESHOLD", "5"))
WALLET_POLL            = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL               = int(os.getenv("DEX_POLL", "60"))
PRICE_WINDOW           = int(os.getenv("PRICE_WINDOW", "3"))
SPIKE_THRESHOLD        = float(os.getenv("SPIKE_THRESHOLD", "8"))
MIN_VOLUME_FOR_ALERT   = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

DISCOVER_ENABLED       = os.getenv("DISCOVER_ENABLED", "true").lower() in ("1","true","yes","on")
DISCOVER_QUERY         = os.getenv("DISCOVER_QUERY", "cronos")
DISCOVER_LIMIT         = int(os.getenv("DISCOVER_LIMIT", "10"))
DISCOVER_POLL          = int(os.getenv("DISCOVER_POLL", "120"))

# Discovery filters (all optional; blanks ignored)
DISCOVER_MIN_LIQ_USD   = float(os.getenv("DISCOVER_MIN_LIQ_USD", "0") or 0)
DISCOVER_MIN_VOL24_USD = float(os.getenv("DISCOVER_MIN_VOL24_USD", "0") or 0)
DISCOVER_MIN_ABS_CHG   = float(os.getenv("DISCOVER_MIN_ABS_CHG", "0") or 0)    # |%| any horizon
DISCOVER_MAX_AGE_H     = int((os.getenv("DISCOVER_MAX_AGE_H") or "0") or 0)    # hours
DISCOVER_REQUIRE_WCRO  = os.getenv("DISCOVER_REQUIRE_WCRO", "false").lower() in ("1","true","yes","on")
DISCOVER_BASE_WHITELIST= [x.strip().upper() for x in (os.getenv("DISCOVER_BASE_WHITELIST","") or "").split(",") if x.strip()]
DISCOVER_BASE_BLACKLIST= [x.strip().upper() for x in (os.getenv("DISCOVER_BASE_BLACKLIST","") or "").split(",") if x.strip()]

# Time / reports
TZ              = os.getenv("TZ", "Europe/Athens")
INTRADAY_HOURS  = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR        = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE      = int(os.getenv("EOD_MINUTE", "59"))

# Alerts Monitor (wallet 24h + optional risky)
ALERTS_INTERVAL_MIN     = int(os.getenv("ALERTS_INTERVAL_MIN", "15"))
DUMP_ALERT_24H_PCT      = float(os.getenv("DUMP_ALERT_24H_PCT", "-15"))
PUMP_ALERT_24H_PCT      = float(os.getenv("PUMP_ALERT_24H_PCT", "20"))

# Guard (per buy)
GUARD_PUMP_FROM_ENTRY   = float(os.getenv("GUARD_PUMP_FROM_ENTRY", "20"))   # +20%
GUARD_DUMP_FROM_ENTRY   = float(os.getenv("GUARD_DUMP_FROM_ENTRY", "-12"))  # -12%
GUARD_TRAIL_DROP_FROM_PK= float(os.getenv("GUARD_TRAIL_DROP_FROM_PK", "-8"))# -8% from peak after pump
GUARD_WINDOW_MIN        = int(os.getenv("GUARD_WINDOW_MIN", "60"))          # track for 60 min after buy
GUARD_CHECK_SEC         = int(os.getenv("GUARD_CHECK_SEC", "60"))           # check guard each 60 sec

# ----------------------- Constants -----------------------
ETHERSCAN_V2_URL  = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID    = 25
DEX_BASE_PAIRS    = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS   = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH   = "https://api.dexscreener.com/latest/dex/search"
COINGECKO_SIMPLE  = "https://api.coingecko.com/api/v3/simple/price"
COINGECKO_TOKEN   = "https://api.coingecko.com/api/v3/simple/token_price/cronos"
CRONOS_TX         = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL      = "https://api.telegram.org/bot{token}/sendMessage"
DATA_DIR          = "/app/data"
ATH_FILE          = os.path.join(DATA_DIR, "ath_prices.json")
PRICE_CACHE_TTL   = 60

# ----------------------- Logging -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wallet-monitor")

# ----------------------- HTTP session -----------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0"})
_rate_lock = threading.Lock()
_last_call = 0.0
def safe_get(url, *, params=None, timeout=12, retries=3, backoff=1.5):
    global _last_call
    for i in range(retries):
        with _rate_lock:
            dt = time.time() - _last_call
            if dt < 0.15:
                time.sleep(0.15 - dt)
            _last_call = time.time()
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            sc = r.status_code
            if sc == 200:
                try:
                    return r.json()
                except Exception:
                    return None
            if sc in (403, 404, 429, 500, 502, 503):
                time.sleep(backoff * (i+1))
                continue
            return None
        except Exception:
            time.sleep(backoff * (i+1))
    return None

def _now():
    # naive local
    return datetime.now()

# ----------------------- Shutdown event -----------------------
shutdown_event = threading.Event()

# ----------------------- State -----------------------
_seen_native_hashes = set()
_seen_token_keys    = set()   # (hash, token_addr, signed_amount) dedup inside day
_last_prices        = {}
_price_history      = {}
_last_pair_tx       = {}
_tracked_pairs      = set()
_known_pairs_meta   = {}

_day_ledger_lock    = threading.Lock()
_token_balances     = defaultdict(float)   # "CRO" or "0x.."
_token_meta         = {}                   # addr -> {symbol, decimals}
_position_qty       = defaultdict(float)   # open qty
_position_cost      = defaultdict(float)   # total cost basis of open
_realized_pnl_today = 0.0

# persist last good price per token (so no 0.0)
_last_good_price    = {}   # key: "CRO" or 0x.. -> float
# per-token ATH
_ath_prices         = {}   # same keys

# Guard state per token address key
_guard_state = {}  # key -> {"entry_price","start_ts","peak_price","pumped":bool}

# Cooldowns to avoid spamming alerts
_alert_cd = {}     # key "type|token" -> last_ts

EPSILON = 1e-12
_last_intraday_sent = 0.0

# Ensure data dir & tz
try: os.makedirs(DATA_DIR, exist_ok=True)
except: pass
try: os.environ["TZ"] = TZ
except: pass

# ----------------------- Utils -----------------------
def ymd(dt=None): return (dt or _now()).strftime("%Y-%m-%d")
def month_prefix(dt=None): return (dt or _now()).strftime("%Y-%m")
def data_file_for_today(): return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

def read_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except: return default

def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(obj,f,ensure_ascii=False,indent=2)
    os.replace(tmp, path)

def fmt_amt(a):
    try: a=float(a)
    except: return str(a)
    if abs(a)>=1: return f"{a:,.4f}"
    if abs(a)>=0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

def fmt_price(p):
    try: p=float(p)
    except: return str(p)
    return f"{p:,.6f}"

def nonzero(x): 
    try: return abs(float(x))>EPSILON
    except: return False

def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: 
        return False
    try:
        url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
        r = SESSION.post(url, data={"chat_id":TELEGRAM_CHAT_ID,"text":text,"parse_mode":"Markdown"}, timeout=12)
        return r.status_code==200
    except Exception as e:
        log.debug("telegram fail: %s", e)
        return False

def cooldown_ok(key: str, secs: int) -> bool:
    last = _alert_cd.get(key, 0.0)
    now  = time.time()
    if now - last >= secs:
        _alert_cd[key] = now
        return True
    return False

# ----------------------- ATH persistence -----------------------
def load_aths():
    d = read_json(ATH_FILE, default={})
    if isinstance(d, dict):
        for k,v in d.items():
            try: _ath_prices[k]=float(v)
            except: pass

def persist_aths():
    write_json(ATH_FILE, _ath_prices)

# ----------------------- Price helpers -----------------------
def _top_price_from_pairs(pairs):
    if not pairs: return None
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")).lower() != "cronos": 
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price<=0: continue
            if liq>best_liq:
                best_liq, best = liq, price
        except: 
            continue
    return best

def price_from_dex_token(addr):
    d = safe_get(f"{DEX_BASE_TOKENS}/cronos/{addr}")
    if d and isinstance(d, dict): 
        return _top_price_from_pairs(d.get("pairs"))
    return None

def price_from_dex_search(q):
    d = safe_get(DEX_BASE_SEARCH, params={"q":q})
    if d and isinstance(d, dict):
        return _top_price_from_pairs(d.get("pairs"))
    return None

def price_from_cg_contract(addr):
    d = safe_get(COINGECKO_TOKEN, params={"contract_addresses":addr,"vs_currencies":"usd"})
    if d and addr in d and "usd" in d[addr]:
        try: return float(d[addr]["usd"])
        except: return None
    return None

def price_from_cg_ids_for_cro():
    d = safe_get(COINGECKO_SIMPLE, params={"ids":"cronos,crypto-com-chain","vs_currencies":"usd"})
    if isinstance(d, dict):
        for k in ("cronos","crypto-com-chain"):
            if k in d and "usd" in d[k]:
                try: return float(d[k]["usd"])
                except: pass
    return None

def get_price_usd(symbol_or_addr: str):
    if not symbol_or_addr: return None
    key = symbol_or_addr.lower()
    # cache
    c = _last_prices.get(key)
    if c and (time.time()-c[1] < PRICE_CACHE_TTL):
        return c[0]
    price = None
    if key in ("cro","wcro","wrappedcro","w-cro","wrapped cro"):
        price = price_from_dex_search("wcro usdt") or price_from_dex_search("cro usdt") or price_from_cg_ids_for_cro()
    elif key.startswith("0x") and len(key)==42:
        price = price_from_dex_token(key) or price_from_cg_contract(key) or price_from_dex_search(key)
    else:
        price = price_from_dex_search(key) or price_from_dex_search(f"{key} usdt")
    _last_prices[key] = (price, time.time())
    return price

def get_token_price(chain: str, token_address: str):
    # robust: tokens/{chain}/{addr} -> tokens/{addr} -> search -> coingecko
    d = safe_get(f"{DEX_BASE_TOKENS}/{chain}/{token_address}")
    if d and isinstance(d, dict) and d.get("pairs"):
        p = _top_price_from_pairs(d["pairs"])
        if p: return float(p)
    d = safe_get(f"{DEX_BASE_TOKENS}/{token_address}")
    if d and isinstance(d, dict) and d.get("pairs"):
        p = _top_price_from_pairs(d["pairs"])
        if p: return float(p)
    p = price_from_dex_search(token_address)
    if p: return float(p)
    cg = price_from_cg_contract(token_address)
    if cg: return float(cg)
    return 0.0

def last_good_price_for(key, fallback=0.0):
    # keep non-zero last for display & alerts
    p = None
    if key == "CRO":
        p = get_price_usd("CRO")
    elif isinstance(key, str) and key.startswith("0x") and len(key)==42:
        p = get_token_price("cronos", key) or get_price_usd(key)
    else:
        p = get_price_usd(key)
    if p and p>0:
        _last_good_price[key] = p
        return p
    # fallback to last_good stored
    lg = _last_good_price.get(key)
    if lg and lg>0: return lg
    return fallback

# ----------------------- Etherscan fetchers -----------------------
def fetch_latest_wallet_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params = {
        "chainid": CRONOS_CHAINID, "module": "account", "action": "txlist",
        "address": WALLET_ADDRESS, "startblock": 0, "endblock": 99999999,
        "page": 1, "offset": limit, "sort": "desc", "apikey": ETHERSCAN_API
    }
    d = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15)
    if d and str(d.get("status",""))=="1" and isinstance(d.get("result"), list):
        return d["result"]
    return []

def fetch_latest_token_txs(limit=50):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params = {
        "chainid": CRONOS_CHAINID, "module": "account", "action": "tokentx",
        "address": WALLET_ADDRESS, "startblock": 0, "endblock": 99999999,
        "page": 1, "offset": limit, "sort": "desc", "apikey": ETHERSCAN_API
    }
    d = safe_get(ETHERSCAN_V2_URL, params=params, timeout=15)
    if d and str(d.get("status",""))=="1" and isinstance(d.get("result"), list):
        return d["result"]
    return []

# ----------------------- Ledger helpers -----------------------
def _append_ledger(entry: dict):
    # dedup on (hash, token_addr, amount)
    with _day_ledger_lock:
        path = data_file_for_today()
        data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        h = entry.get("txhash")
        ta = entry.get("token_addr")
        amt= float(entry.get("amount") or 0.0)
        # check dup
        for e in data["entries"]:
            if e.get("txhash")==h and (e.get("token_addr")==ta) and abs(float(e.get("amount") or 0.0)-amt)<1e-15:
                return
        data["entries"].append(entry)
        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
        data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0))
        write_json(path, data)

def _replay_today_cost_basis():
    global _position_qty, _position_cost, _realized_pnl_today
    _position_qty.clear(); _position_cost.clear(); _realized_pnl_today=0.0
    data = read_json(data_file_for_today(), default=None)
    if not isinstance(data, dict): return
    for e in data.get("entries", []):
        key = "CRO" if (e.get("token_addr") in (None, "", "None") and (e.get("token") == "CRO")) else (e.get("token_addr") or "CRO")
        amt = float(e.get("amount") or 0.0)
        price = float(e.get("price_usd") or 0.0)
        rp = _update_cost_basis(key, amt, price)
        e["realized_pnl"] = rp
    try:
        data["realized_pnl"] = sum(float(x.get("realized_pnl",0.0)) for x in data.get("entries",[]))
        write_json(data_file_for_today(), data)
    except: pass

# ----------------------- Cost-basis / PnL -----------------------
def _update_cost_basis(token_key: str, signed_amount: float, price_usd: float):
    global _realized_pnl_today
    qty = _position_qty[token_key]; cost = _position_cost[token_key]
    realized = 0.0
    if signed_amount > EPSILON:
        add_cost = signed_amount * max(0.0, price_usd or 0.0)
        _position_qty[token_key]  = qty + signed_amount
        _position_cost[token_key] = cost + add_cost
    elif signed_amount < -EPSILON:
        sell_qty = min(-signed_amount, qty) if qty>EPSILON else 0.0
        avg_cost = (cost/qty) if qty>EPSILON else max(0.0, price_usd or 0.0)
        realized = (max(0.0, price_usd) - avg_cost) * sell_qty
        _position_qty[token_key]  = max(0.0, qty - sell_qty)
        _position_cost[token_key] = max(0.0, cost - avg_cost * sell_qty)
    _realized_pnl_today += realized
    return realized

# ----------------------- Handlers -----------------------
def handle_native_tx(tx: dict):
    h = tx.get("hash")
    if not h or h in _seen_native_hashes: return
    _seen_native_hashes.add(h)

    val_raw = tx.get("value","0")
    try: amount_cro = int(val_raw)/10**18
    except:
        try: amount_cro=float(val_raw)
        except: amount_cro=0.0

    frm = (tx.get("from") or "").lower()
    to  = (tx.get("to") or "").lower()
    ts  = int(tx.get("timeStamp") or 0)
    dt  = datetime.fromtimestamp(ts) if ts>0 else _now()

    sign = +1 if to==WALLET_ADDRESS else (-1 if frm==WALLET_ADDRESS else 0)
    if sign==0 or abs(amount_cro)<=EPSILON: return

    price = last_good_price_for("CRO", 0.0)
    usd_value = sign * amount_cro * (price or 0.0)

    _token_balances["CRO"] += sign * amount_cro
    _token_meta["CRO"] = {"symbol":"CRO","decimals":18}
    realized = _update_cost_basis("CRO", sign*amount_cro, price or 0.0)

    link = CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\n"
        f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount_cro:.6f} CRO\n"
        f"Price: ${fmt_price(price or 0)}\nUSD value: ${fmt_amt(usd_value)}"
    )
    _append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"), "txhash": h, "type": "native",
        "token":"CRO","token_addr": None, "amount": sign*amount_cro,
        "price_usd": price or 0.0, "usd_value": usd_value, "realized_pnl": realized,
        "from": frm, "to": to,
    })

def _key_for_token(taddr, sym):
    return taddr.lower() if (taddr and taddr.startswith("0x") and len(taddr)==42) else (sym or "UNKNOWN").upper()

def _guard_on_buy(key, entry_price):
    if entry_price and entry_price>0:
        _guard_state[key] = {
            "entry_price": entry_price,
            "start_ts": time.time(),
            "peak_price": entry_price,
            "pumped": False
        }

def _mini_summary(key, symbol, signed_amount):
    # show after each trade
    live = last_good_price_for(key, 0.0)
    qty  = _position_qty.get(key, 0.0)
    cost = _position_cost.get(key, 0.0)
    avg  = (cost/qty) if qty>EPSILON else (live or 0.0)
    unreal = max(0.0, live)*qty - cost if qty>EPSILON and live>0 else 0.0
    dirword = "BUY" if signed_amount>0 else "SELL"
    send_telegram(
        f"• {dirword} {symbol} {_format_signed(signed_amount)} @ live ${fmt_price(live)}\n"
        f"   Open: {fmt_amt(qty)} {symbol} | Avg: ${fmt_price(avg)} | Unreal: ${fmt_amt(unreal)}"
    )

def _format_signed(x):
    try:
        return f"{float(x):,.6f}"
    except:
        return str(x)

def _update_ath_and_alert(key, symbol):
    p = last_good_price_for(key, 0.0)
    if not p or p<=0: return
    prev = _ath_prices.get(key, 0.0)
    if p > max(0.0, prev):
        _ath_prices[key] = p
        persist_aths()
        send_telegram(f"🏆 New ATH {symbol}: ${fmt_price(p)}")

def handle_erc20_tx(t: dict):
    h = t.get("hash")
    if not h: return
    frm = (t.get("from") or "").lower()
    to  = (t.get("to") or "").lower()
    if WALLET_ADDRESS not in (frm, to): return

    token_addr = (t.get("contractAddress") or "").lower()
    symbol     = (t.get("tokenSymbol") or (token_addr[:8] if token_addr else "TOKEN")).upper()
    try: decimals = int(t.get("tokenDecimal") or 18)
    except: decimals = 18

    val_raw = t.get("value", "0")
    try: amount = int(val_raw) / (10**decimals)
    except:
        try: amount=float(val_raw)
        except: amount=0.0

    ts = int(t.get("timeStamp") or 0)
    dt = datetime.fromtimestamp(ts) if ts>0 else _now()
    sign = +1 if to==WALLET_ADDRESS else -1

    key = _key_for_token(token_addr, symbol)
    # prefer contract price
    if token_addr and token_addr.startswith("0x") and len(token_addr)==42:
        price = last_good_price_for(token_addr, 0.0)
    else:
        price = last_good_price_for(symbol, 0.0)
    usd_value = sign * amount * (price or 0.0)

    # balances/meta
    _token_balances[key] += sign * amount
    _token_meta[key] = {"symbol": symbol, "decimals": decimals}
    realized = _update_cost_basis(key, sign*amount, price or 0.0)

    # ATH update
    _update_ath_and_alert(key, symbol)

    # Mini summary + Guard
    _mini_summary(key, symbol, sign*amount)
    if sign>0: _guard_on_buy(key, price or 0.0)

    link = CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Token TX* ({'IN' if sign>0 else 'OUT'}) {symbol}\n"
        f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {symbol}\n"
        f"Price: ${fmt_price(price or 0)}\nUSD value: ${fmt_amt(usd_value)}"
    )

    # Dedup ledger by (hash, token_addr/symbol key, amount)
    _append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h, "type":"erc20",
        "token": symbol, "token_addr": (token_addr if token_addr else key),
        "amount": sign*amount, "price_usd": price or 0.0,
        "usd_value": usd_value, "realized_pnl": realized, "from": frm, "to": to,
    })

# ----------------------- Wallet monitor loop -----------------------
def wallet_monitor_loop():
    log.info("Wallet monitor starting; loading initial recent txs...")
    # seed seen hashes to avoid spam on cold start
    initial = fetch_latest_wallet_txs(limit=50)
    for tx in initial:
        h=tx.get("hash")
        if h: _seen_native_hashes.add(h)
    _replay_today_cost_basis()
    send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}` (Cronos).")

    last_tok_hashes = set()
    while not shutdown_event.is_set():
        try:
            txs = fetch_latest_wallet_txs(limit=25)
            for tx in reversed(txs or []):
                handle_native_tx(tx)
        except Exception as e:
            log.debug("native loop err: %s", e)
        try:
            toks = fetch_latest_token_txs(limit=80)
            for t in reversed(toks or []):
                hk = (t.get("hash"), (t.get("contractAddress") or "").lower(), float((int(t.get("value") or "0")) / (10**int(t.get("tokenDecimal") or 18)) if (t.get("value") and t.get("tokenDecimal")) else 0.0))
                # soft dedup per runtime
                if hk in last_tok_hashes: continue
                handle_erc20_tx(t)
                last_tok_hashes.add(hk)
            if len(last_tok_hashes)>800:
                last_tok_hashes = set(list(last_tok_hashes)[-500:])
        except Exception as e:
            log.debug("erc20 loop err: %s", e)

        for _ in range(WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Dexscreener pair monitor + discovery -----------------------
def slug(chain, pair_addr): return f"{chain}/{pair_addr}".lower()

def fetch_pair(slg):
    return safe_get(f"{DEX_BASE_PAIRS}/{slg}", timeout=12)

def fetch_token_pairs(chain, token_address):
    d = safe_get(f"{DEX_BASE_TOKENS}/{chain}/{token_address}", timeout=12)
    if isinstance(d, dict) and isinstance(d.get("pairs"), list):
        return d["pairs"]
    return []

def fetch_search(query):
    d = safe_get(DEX_BASE_SEARCH, params={"q":query}, timeout=12)
    if isinstance(d, dict) and isinstance(d.get("pairs"), list):
        return d["pairs"]
    return []

def ensure_tracking_pair(chain, pair_address, meta=None):
    s = slug(chain, pair_address)
    if s in _tracked_pairs: return
    _tracked_pairs.add(s)
    _last_prices[s]   = None
    _last_pair_tx[s]  = None
    _price_history[s] = deque(maxlen=PRICE_WINDOW)
    if meta: _known_pairs_meta[s] = meta
    sym = None
    if isinstance(meta, dict):
        bt = meta.get("baseToken") or {}
        sym = bt.get("symbol")
    title = f"{sym} ({s})" if sym else s
    ds_link = f"https://dexscreener.com/{chain}/{pair_address}"
    send_telegram(f"🆕 Now monitoring pair: {title}\n{ds_link}")

def update_price_history(slg, price):
    hist = _price_history.get(slg)
    if hist is None:
        hist = deque(maxlen=PRICE_WINDOW); _price_history[slg]=hist
    hist.append(price); _last_prices[slg]=price

def detect_spike(slg):
    hist = _price_history.get(slg)
    if not hist or len(hist)<2: return None
    first, last = hist[0], hist[-1]
    if not first: return None
    pct = (last-first)/first*100.0
    return pct if abs(pct)>=SPIKE_THRESHOLD else None

def _pair_passes_filters(p):
    try:
        if str(p.get("chainId","")).lower()!="cronos": return False
        base = (p.get("baseToken") or {}).get("symbol","").upper()
        quote= (p.get("quoteToken") or {}).get("symbol","").upper()
        if DISCOVER_REQUIRE_WCRO and quote not in ("WCRO","CRO"): return False
        if DISCOVER_BASE_WHITELIST and base not in DISCOVER_BASE_WHITELIST: return False
        if DISCOVER_BASE_BLACKLIST and base in DISCOVER_BASE_BLACKLIST: return False
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        vol = float((p.get("volume") or {}).get("h24") or 0)
        if DISCOVER_MIN_LIQ_USD and liq < DISCOVER_MIN_LIQ_USD: return False
        if DISCOVER_MIN_VOL24_USD and vol < DISCOVER_MIN_VOL24_USD: return False
        pc = p.get("priceChange") or {}
        anychg = 0.0
        for k in ("m5","h1","h4","h6","h24"):
            try:
                v = float(pc.get(k) or 0)
                anychg = max(anychg, abs(v))
            except: pass
        if DISCOVER_MIN_ABS_CHG and anychg < DISCOVER_MIN_ABS_CHG: return False
        if DISCOVER_MAX_AGE_H:
            ms = int(p.get("pairCreatedAt") or 0)
            if ms>0:
                age_h = max(0.0, (time.time()*1000 - ms)/1000/3600)
                if age_h > DISCOVER_MAX_AGE_H: return False
        return True
    except:
        return False

def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        log.info("No tracked pairs; monitor waits until discovery/seed adds some.")
    while not shutdown_event.is_set():
        if not _tracked_pairs:
            time.sleep(DEX_POLL); continue
        for s in list(_tracked_pairs):
            try:
                d = fetch_pair(s)
                if not d: continue
                pair = d.get("pair") or (d.get("pairs")[0] if d.get("pairs") else None)
                if not pair: continue
                price_val = None
                try: price_val = float(pair.get("priceUsd") or 0)
                except: price_val=None
                vol_h1=None
                try: vol_h1=float((pair.get("volume") or {}).get("h1") or 0)
                except: pass
                symbol = (pair.get("baseToken") or {}).get("symbol") or s
                if price_val and price_val>0:
                    update_price_history(s, price_val)
                    spike_pct = detect_spike(s)
                    if spike_pct is not None:
                        if MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1<MIN_VOLUME_FOR_ALERT:
                            pass
                        else:
                            send_telegram(f"🚨 Spike on {symbol}: {spike_pct:.2f}%\nPrice: ${price_val:.6f}")
                            _price_history[s].clear(); _last_prices[s]=price_val
                prev = _last_prices.get(s, None)
                if prev and price_val and prev!=0:
                    delta = (price_val - prev)/prev*100.0
                    if abs(delta) >= PRICE_MOVE_THRESHOLD:
                        send_telegram(f"📈 Price move on {symbol}: {delta:.2f}%\nPrice: ${price_val:.6f} (prev ${prev:.6f})")
                        _last_prices[s] = price_val
                last_tx = (pair.get("lastTx") or {}).get("hash")
                if last_tx:
                    if _last_pair_tx.get(s) != last_tx:
                        _last_pair_tx[s]=last_tx
                        send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx)}")
            except Exception as e:
                log.debug("pairs loop err %s", e)
        for _ in range(DEX_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

def discovery_loop():
    # seed from DEX_PAIRS
    for s in [p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]:
        if s.startswith("cronos/"):
            ensure_tracking_pair("cronos", s.split("/",1)[1])
    # seed from TOKENS -> resolve top pair
    for t in [x.strip().lower() for x in (TOKENS or "").split(",") if x.strip()]:
        if not t.startswith("cronos/"): continue
        _, token_addr = t.split("/",1)
        pairs = fetch_token_pairs("cronos", token_addr)
        if pairs:
            p = pairs[0]; pa = p.get("pairAddress")
            if pa: ensure_tracking_pair("cronos", pa, meta=p)
    if not DISCOVER_ENABLED:
        log.info("Discovery disabled."); return
    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos) with filters.")
    while not shutdown_event.is_set():
        try:
            found = fetch_search(DISCOVER_QUERY) or []
            adopted=0
            for p in found:
                if not _pair_passes_filters(p): continue
                pa = p.get("pairAddress")
                if not pa: continue
                s = slug("cronos", pa)
                if s in _tracked_pairs: continue
                ensure_tracking_pair("cronos", pa, meta=p)
                adopted += 1
                if adopted >= DISCOVER_LIMIT: break
        except Exception as e:
            log.debug("discovery err: %s", e)
        for _ in range(DISCOVER_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Wallet snapshot & holdings -----------------------
def get_wallet_balances_snapshot(addr=None):
    balances={}
    address=(addr or WALLET_ADDRESS or "").lower()
    if CRONOSCAN_API:
        try:
            d = safe_get("https://api.cronoscan.com/api",
                         params={"module":"account","action":"tokenlist","address":address,"apikey":CRONOSCAN_API}, timeout=12)
            if isinstance(d, dict) and isinstance(d.get("result"), list):
                for tok in d["result"]:
                    sym = tok.get("symbol") or tok.get("tokenSymbol") or tok.get("name") or ""
                    try: dec = int(tok.get("decimals") or tok.get("tokenDecimal") or 18)
                    except: dec = 18
                    bal_raw = tok.get("balance") or tok.get("tokenBalance") or "0"
                    try: amt = int(bal_raw)/(10**dec)
                    except:
                        try: amt=float(bal_raw)
                        except: amt=0.0
                    if sym: balances[sym]=balances.get(sym,0.0)+amt
        except Exception as e:
            log.debug("cronoscan snapshot err: %s", e)
    # merge runtime
    for k,v in _token_balances.items():
        if k=="CRO":
            balances["CRO"]=balances.get("CRO",0.0)+v
        else:
            sym = (_token_meta.get(k,{}).get("symbol")) or (k[:8] if isinstance(k,str) else "TOKEN")
            balances[sym]=balances.get(sym,0.0)+v
    if "CRO" not in balances:
        balances["CRO"]=float(_token_balances.get("CRO",0.0))
    return balances

def compute_holdings_usd():
    total=0.0; breakdown=[]; unreal=0.0
    # CRO
    cro_amt=max(0.0, _token_balances.get("CRO",0.0))
    if cro_amt>EPSILON:
        cro_p = last_good_price_for("CRO", 0.0)
        cro_val = cro_amt*(cro_p or 0.0)
        total+=cro_val
        breakdown.append({"token":"CRO","token_addr":"CRO","amount":cro_amt,"price_usd":cro_p or 0.0,"usd_value":cro_val})
        rq=_position_qty.get("CRO",0.0); rc=_position_cost.get("CRO",0.0)
        if rq>EPSILON and (cro_p or 0)>0:
            unreal += (cro_amt*(cro_p or 0.0) - rc)
    # tokens
    for addr, amt in list(_token_balances.items()):
        if addr=="CRO": continue
        amt=max(0.0,amt)
        if amt<=EPSILON: continue
        sym = _token_meta.get(addr,{}).get("symbol") or (addr[:8] if isinstance(addr,str) else "TOKEN")
        if isinstance(addr,str) and addr.startswith("0x") and len(addr)==42:
            p = last_good_price_for(addr, 0.0)
        else:
            p = last_good_price_for(sym, 0.0)
        val = amt*(p or 0.0)
        total+=val
        breakdown.append({"token":sym,"token_addr":addr,"amount":amt,"price_usd":p or 0.0,"usd_value":val})
        rq=_position_qty.get(addr,0.0); rc=_position_cost.get(addr,0.0)
        if rq>EPSILON and (p or 0)>0:
            unreal += (amt*(p or 0.0) - rc)
    return total, breakdown, unreal

# ----------------------- Month aggregates -----------------------
def sum_month_net_and_real():
    pref = month_prefix()
    total_flow=0.0; total_real=0.0
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json") and pref in fn:
                d = read_json(os.path.join(DATA_DIR,fn), default=None)
                if isinstance(d, dict):
                    total_flow += float(d.get("net_usd_flow",0.0))
                    total_real += float(d.get("realized_pnl",0.0))
    except: pass
    return total_flow, total_real

# ----------------------- Per-asset summarize (today, clean) -----------------------
def summarize_today_per_asset():
    d = read_json(data_file_for_today(), default={"entries": [], "date": ymd(), "net_usd_flow":0.0, "realized_pnl":0.0})
    entries = d.get("entries", [])
    per_flow   = defaultdict(float)
    per_real   = defaultdict(float)
    per_qty    = defaultdict(float)  # net qty today (+in/-out)
    per_last_px= {}
    per_addr   = {}

    for e in entries:
        tok = e.get("token") or "?"
        addr= e.get("token_addr") or tok
        per_flow[tok] += float(e.get("usd_value") or 0.0)
        per_real[tok] += float(e.get("realized_pnl") or 0.0)
        per_qty[tok]  += float(e.get("amount") or 0.0)
        px = float(e.get("price_usd") or 0.0)
        if px>0: per_last_px[tok]=px
        if addr and tok not in per_addr: per_addr[tok]=addr

    out=[]
    for tok in per_flow.keys():
        addr = per_addr.get(tok, tok)
        # live price preferring contract
        if isinstance(addr,str) and addr.startswith("0x") and len(addr)==42:
            live = last_good_price_for(addr, per_last_px.get(tok, 0.0))
        else:
            live = last_good_price_for(tok, per_last_px.get(tok, 0.0))
        net_qty_today = per_qty.get(tok, 0.0)

        # open qty now from global (only positive -> unreal)
        key = addr if (isinstance(addr,str) and addr.startswith("0x") and len(addr)==42) else tok
        open_qty_now = max(0.0, _position_qty.get(key, 0.0))
        open_cost    = _position_cost.get(key, 0.0)
        unreal = 0.0
        if open_qty_now>EPSILON and (live or 0)>0:
            unreal = open_qty_now*(live or 0.0) - open_cost

        out.append({
            "token": tok, "addr": addr, "flow": per_flow[tok],
            "realized": per_real[tok], "today_qty": net_qty_today,
            "price": live or 0.0, "unreal": unreal
        })
    # sort by abs(flow)
    out.sort(key=lambda x: abs(x["flow"]), reverse=True)
    return out

# ----------------------- Report builder (daily/intraday) -----------------------
def build_day_report_text():
    d = read_json(data_file_for_today(), default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    entries = d.get("entries", [])
    net_flow = float(d.get("net_usd_flow", 0.0))
    realized_today = float(d.get("realized_pnl", 0.0))

    lines = [f"*📒 Daily Report* ({d.get('date')})"]
    if not entries:
        lines.append("_No transactions today._")
    else:
        lines.append("*Transactions:*")
        MAXL = 20
        for e in entries[-MAXL:]:
            tok = e.get("token") or "?"
            amt = e.get("amount") or 0.0
            usd = e.get("usd_value") or 0.0
            tm  = (e.get("time","")[-8:] if e.get("time") else "")
            unit= e.get("price_usd") or 0.0
            rp  = float(e.get("realized_pnl",0.0) or 0.0)
            pnl = f"  PnL: ${fmt_amt(rp)}" if abs(rp)>1e-9 else ""
            lines.append(f"• {tm} — {'IN' if float(amt)>0 else 'OUT'} {tok} {fmt_amt(amt)}  @ ${fmt_price(unit)}  (${fmt_amt(usd)}){pnl}")
        if len(entries)>MAXL:
            lines.append(f"_…and {len(entries)-MAXL} earlier txs._")

    lines.append(f"\n*Net USD flow today:* ${fmt_amt(net_flow)}")
    lines.append(f"*Realized PnL today:* ${fmt_amt(realized_today)}")

    total, breakdown, unreal = compute_holdings_usd()
    lines.append(f"*Holdings (MTM) now:* ${fmt_amt(total)}")
    if breakdown:
        for b in breakdown[:15]:
            lines.append(f"  – {b['token']}: {fmt_amt(b['amount'])} @ ${fmt_price(b['price_usd'])} = ${fmt_amt(b['usd_value'])}")
        if len(breakdown)>15:
            lines.append(f"  …and {len(breakdown)-15} more.")
    lines.append(f"*Unrealized PnL (open positions):* ${fmt_amt(unreal)}")

    pa = summarize_today_per_asset()
    if pa:
        lines.append("\n*Per-Asset Summary (Today):*")
        for x in pa[:12]:
            extra = ""
            if x["today_qty"]>EPSILON:
                extra += f" | today qty {fmt_amt(x['today_qty'])}"
            if x["unreal"] and abs(x["unreal"])>1e-9:
                extra += f" | unreal ${fmt_amt(x['unreal'])}"
            lines.append(f"  • {x['token']}: flow ${fmt_amt(x['flow'])} | realized ${fmt_amt(x['realized'])}{extra} | price ${fmt_price(x['price'])}")
        if len(pa)>12:
            lines.append(f"  …and {len(pa)-12} more.")

    mflow, mreal = sum_month_net_and_real()
    lines.append(f"\n*Month Net Flow:* ${fmt_amt(mflow)}")
    lines.append(f"*Month Realized PnL:* ${fmt_amt(mreal)}")
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
            log.debug("intraday err: %s", e)
        for _ in range(30):
            if shutdown_event.is_set(): break
            time.sleep(1)

def end_of_day_scheduler_loop():
    send_telegram(f"🕛 End-of-day scheduler active (at {EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ}).")
    while not shutdown_event.is_set():
        now = _now()
        tgt = now.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)
        if now>tgt: tgt += timedelta(days=1)
        sleep_s = (tgt-now).total_seconds()
        while sleep_s>0 and not shutdown_event.is_set():
            step=min(30, sleep_s); time.sleep(step); sleep_s -= step
        if shutdown_event.is_set(): break
        try:
            txt = build_day_report_text()
            send_telegram("🟢 *End of Day Report*\n" + txt)
        except Exception as e:
            log.debug("eod err: %s", e)

# ----------------------- Reconciliation helper (basic) -----------------------
def reconcile_swaps_from_entries():
    d = read_json(data_file_for_today(), default={"entries": []})
    entries = d.get("entries", [])
    swaps=[]
    i=0
    while i<len(entries):
        e = entries[i]
        if float(e.get("amount",0.0))<0:
            # search forward a different token IN
            for j in range(i+1, min(i+7, len(entries))):
                e2=entries[j]
                if float(e2.get("amount",0.0))>0 and e2.get("token")!=e.get("token"):
                    swaps.append((e,e2)); break
        i+=1
    return swaps

# ----------------------- Alerts Monitor (wallet 24h) -----------------------
def _pair_for_token(addr_or_sym):
    # find a Cronos pair for a token (prefer contract)
    if isinstance(addr_or_sym, str) and addr_or_sym.startswith("0x") and len(addr_or_sym)==42:
        pairs = fetch_token_pairs("cronos", addr_or_sym)
        if pairs: return pairs[0]
    # fallback: search by symbol
    pairs = fetch_search(addr_or_sym)
    if pairs:
        for p in pairs:
            if str(p.get("chainId","")).lower()=="cronos":
                return p
    return None

def alerts_monitor_loop():
    send_telegram(f"🛰 Alerts monitor active every {ALERTS_INTERVAL_MIN}m. Wallet 24h dump/pump: {DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT}.")
    while not shutdown_event.is_set():
        try:
            # watch all tokens that have positive holdings or moved today
            watch=set()
            # holdings
            for k,amt in _token_balances.items():
                if amt>EPSILON: watch.add(k)
            # tokens moved today
            d = read_json(data_file_for_today(), default={"entries":[]})
            for e in d.get("entries", []):
                addr = e.get("token_addr") or e.get("token") or ""
                if addr: watch.add(addr)
            # CRO always
            watch.add("CRO")

            for key in list(watch):
                # skip dead (price 0)
                p = last_good_price_for(key, 0.0)
                if not p or p<=0: 
                    continue
                pair = None
                if isinstance(key,str) and key.startswith("0x") and len(key)==42:
                    pair = _pair_for_token(key)
                else:
                    pair = _pair_for_token(key)
                if not pair: 
                    continue
                price_change = (pair.get("priceChange") or {})
                pc24 = None
                try: pc24 = float(price_change.get("h24") or 0.0)
                except: pc24 = None

                symbol = (pair.get("baseToken") or {}).get("symbol") or (_token_meta.get(key,{}).get("symbol") or (key[:8] if isinstance(key,str) else "TOKEN"))
                pair_addr = pair.get("pairAddress")
                ds_link = f"https://dexscreener.com/cronos/{pair_addr}" if pair_addr else ""

                if pc24 is not None:
                    # dump
                    if pc24 <= DUMP_ALERT_24H_PCT and cooldown_ok(f"dump24|{symbol}", 30*60):
                        send_telegram(f"⚠️ *Dump Alert* {symbol} 24h {pc24:.2f}%\nPrice ${fmt_price(p)}\n{ds_link}")
                    # pump
                    if pc24 >= PUMP_ALERT_24H_PCT and cooldown_ok(f"pump24|{symbol}", 30*60):
                        send_telegram(f"🚀 *Pump Alert* {symbol} 24h {pc24:.2f}%\nPrice ${fmt_price(p)}\n{ds_link}")

        except Exception as e:
            log.debug("alerts err: %s", e)

        for _ in range(ALERTS_INTERVAL_MIN*60):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Guard monitor (after buy) -----------------------
def guard_monitor_loop():
    send_telegram(f"🛡 Guard monitor active: {GUARD_WINDOW_MIN}m window, alert below {GUARD_DUMP_FROM_ENTRY}% / above {GUARD_PUMP_FROM_ENTRY}% / trailing {GUARD_TRAIL_DROP_FROM_PK}%.")
    while not shutdown_event.is_set():
        try:
            now = time.time()
            remove=[]
            for key, st in list(_guard_state.items()):
                if not isinstance(st, dict): 
                    remove.append(key); continue
                if now - st.get("start_ts", now) > GUARD_WINDOW_MIN*60:
                    remove.append(key); continue
                # live price
                live = last_good_price_for(key, 0.0)
                if not live or live<=0: 
                    continue
                entry = st.get("entry_price", 0.0) or 0.0
                if entry<=0: 
                    continue
                # peak update
                if live > st.get("peak_price", entry):
                    st["peak_price"] = live
                # checks
                chg_from_entry = (live-entry)/entry*100.0
                if (not st.get("pumped")) and chg_from_entry >= GUARD_PUMP_FROM_ENTRY and cooldown_ok(f"guard_pump|{key}", 10*60):
                    st["pumped"]=True
                    sym = _token_meta.get(key,{}).get("symbol") or (key[:8] if isinstance(key,str) else "TOKEN")
                    send_telegram(f"🟢 GUARD Pump {sym} +{chg_from_entry:.2f}% from entry (${fmt_price(entry)} → ${fmt_price(live)})")
                if chg_from_entry <= GUARD_DUMP_FROM_ENTRY and cooldown_ok(f"guard_dump|{key}", 10*60):
                    sym = _token_meta.get(key,{}).get("symbol") or (key[:8] if isinstance(key,str) else "TOKEN")
                    send_telegram(f"🔴 GUARD Dump {sym} {chg_from_entry:.2f}% from entry (${fmt_price(entry)} → ${fmt_price(live)})")
                if st.get("pumped"):
                    peak = st.get("peak_price", entry)
                    if peak>0:
                        drop = (live-peak)/peak*100.0
                        if drop <= GUARD_TRAIL_DROP_FROM_PK and cooldown_ok(f"guard_trail|{key}", 10*60):
                            sym = _token_meta.get(key,{}).get("symbol") or (key[:8] if isinstance(key,str) else "TOKEN")
                            send_telegram(f"🟠 GUARD Trailing {sym} {drop:.2f}% from peak (${fmt_price(peak)} → ${fmt_price(live)})")
            for k in remove:
                _guard_state.pop(k, None)
        except Exception as e:
            log.debug("guard err: %s", e)

        for _ in range(GUARD_CHECK_SEC):
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
    log.info("Starting monitor with config:")
    log.info("WALLET_ADDRESS: %s", WALLET_ADDRESS)
    log.info("TELEGRAM_BOT_TOKEN present: %s", bool(TELEGRAM_BOT_TOKEN))
    log.info("TELEGRAM_CHAT_ID: %s", TELEGRAM_CHAT_ID)
    log.info("ETHERSCAN_API present: %s", bool(ETHERSCAN_API))
    log.info("DEX_PAIRS: %s", DEX_PAIRS)
    log.info("DISCOVER_ENABLED: %s | DISCOVER_QUERY: %s", DISCOVER_ENABLED, DISCOVER_QUERY)
    log.info("TZ: %s | INTRADAY_HOURS: %s | EOD: %02d:%02d", TZ, INTRADAY_HOURS, EOD_HOUR, EOD_MINUTE)
    log.info("Alerts interval: %sm | Wallet 24h dump/pump: %s/%s", ALERTS_INTERVAL_MIN, DUMP_ALERT_24H_PCT, PUMP_ALERT_24H_PCT)

    load_aths()

    threads=[]
    threads.append(run_with_restart(wallet_monitor_loop, "wallet_monitor"))
    threads.append(run_with_restart(monitor_tracked_pairs_loop, "pairs_monitor"))
    threads.append(run_with_restart(discovery_loop, "discovery"))
    threads.append(run_with_restart(intraday_report_loop, "intraday_report"))
    threads.append(run_with_restart(end_of_day_scheduler_loop, "eod_scheduler"))
    threads.append(run_with_restart(alerts_monitor_loop, "alerts_monitor"))
    threads.append(run_with_restart(guard_monitor_loop, "guard_monitor"))

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
