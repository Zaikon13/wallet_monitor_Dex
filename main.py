#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Wallet monitor (Cronos via Etherscan v2) + Dexscreener Live Scanner
Auto-discovery, PnL (realized & unrealized), intraday/EOD reports, ATH tracking,
swap reconciliation, 24h alerts (όλα τα assets) & Guard alerts μετά από BUY
(pump/dump από entry + trailing από peak).
Drop-in για Railway worker. Χρησιμοποιεί ENV (χωρίς hardcoded secrets).
"""

import os, sys, time, json, signal, threading, logging
from collections import deque, defaultdict
from datetime import datetime, timedelta, timezone
import math
import requests

# ----------------------- ENV -----------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS", "").strip().lower())
ETHERSCAN_API      = os.getenv("ETHERSCAN_API", "")
CRONOSCAN_API      = os.getenv("CRONOSCAN_API", "")  # optional snapshot
TOKENS             = os.getenv("TOKENS", "")         # optional seeding (cronos/0x...)
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD", "5"))
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL           = int(os.getenv("DEX_POLL", "60"))
PRICE_WINDOW       = int(os.getenv("PRICE_WINDOW", "3"))
SPIKE_THRESHOLD    = float(os.getenv("SPIKE_THRESHOLD", "8"))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT", "0"))

DISCOVER_ENABLED   = os.getenv("DISCOVER_ENABLED","true").lower() in ("1","true","yes","on")
DISCOVER_QUERY     = os.getenv("DISCOVER_QUERY","cronos")
DISCOVER_LIMIT     = int(os.getenv("DISCOVER_LIMIT","10"))
DISCOVER_POLL      = int(os.getenv("DISCOVER_POLL","120"))

# Discovery Filters (όλα προαιρετικά)
DISCOVER_MIN_LIQ_USD     = float(os.getenv("DISCOVER_MIN_LIQ_USD","30000"))
DISCOVER_MIN_VOL24_USD   = float(os.getenv("DISCOVER_MIN_VOL24_USD","5000"))
DISCOVER_MIN_ABS_CHG_PCT = float(os.getenv("DISCOVER_MIN_ABS_CHG_PCT","10"))
DISCOVER_REQUIRE_WCRO_QUOTE = os.getenv("DISCOVER_REQUIRE_WCRO_QUOTE","false").lower() in ("1","true","yes","on")
DISCOVER_WHITELIST_BASE  = {s.strip().upper() for s in os.getenv("DISCOVER_WHITELIST_BASE","").split(",") if s.strip()}
DISCOVER_BLACKLIST_BASE  = {s.strip().upper() for s in os.getenv("DISCOVER_BLACKLIST_BASE","").split(",") if s.strip()}
DISCOVER_MAX_PAIR_AGE_HOURS = int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS","24") or "24")

TZ               = os.getenv("TZ","Europe/Athens")
INTRADAY_HOURS   = float(os.getenv("INTRADAY_HOURS","3"))
EOD_HOUR         = int(os.getenv("EOD_HOUR","23"))
EOD_MINUTE       = int(os.getenv("EOD_MINUTE","59"))

# Alerts (24h για όλα τα assets)
ALERTS_INTERVAL_MIN   = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
DUMP_ALERT_24H_PCT    = float(os.getenv("DUMP_ALERT_24H_PCT","-15"))
PUMP_ALERT_24H_PCT    = float(os.getenv("PUMP_ALERT_24H_PCT","20"))

# Guard μετά από BUY (entry/peak based)
GUARD_WINDOW_MIN          = int(os.getenv("GUARD_WINDOW_MIN","60"))
GUARD_PUMP_FROM_ENTRY     = float(os.getenv("GUARD_PUMP_FROM_ENTRY","20"))
GUARD_DUMP_FROM_ENTRY     = float(os.getenv("GUARD_DUMP_FROM_ENTRY","-12"))
GUARD_TRAIL_DROP_FROM_PK  = float(os.getenv("GUARD_TRAIL_DROP_FROM_PK","-8"))

# ----------------------- Constants -----------------------
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
CG_PRICE_SIMPLE  = "https://api.coingecko.com/api/v3/simple"
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"
TELEGRAM_URL     = "https://api.telegram.org/bot{token}/sendMessage"
DATA_DIR         = "/app/data"

# ----------------------- Logging -----------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("wallet-monitor")

# ----------------------- HTTP session + rate-limit -----------------------
SESSION = requests.Session()
SESSION.headers.update({"User-Agent":"Mozilla/5.0 (X11; Linux x86_64)"})
_last_http_ts = 0.0
HTTP_RPS = 5.0  # ~ best-effort
def _throttle():
    global _last_http_ts
    min_gap = 1.0/HTTP_RPS
    dt = time.time()-_last_http_ts
    if dt < min_gap:
        time.sleep(min_gap-dt)
    _last_http_ts = time.time()

def safe_get(url, *, params=None, timeout=15, tries=3, backoff=1.6):
    for i in range(tries):
        try:
            _throttle()
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception:
                    return None
            if r.status_code in (404, 429, 503):
                time.sleep(backoff**i)
                continue
            return None
        except Exception:
            time.sleep(backoff**i)
    return None

# ----------------------- Shutdown / state -----------------------
shutdown_event = threading.Event()

_seen_native = set()
_seen_erc20  = set()

_token_balances = defaultdict(float)     # "CRO" or contract -> qty
_token_meta     = dict()                 # contract -> {symbol,decimals}
_position_qty   = defaultdict(float)     # open qty per key
_position_cost  = defaultdict(float)     # book cost per key
_realized_pnl_today = 0.0

_price_history = dict()                  # pair slug -> deque
_last_prices   = dict()                  # pair slug -> last price
_last_pair_tx  = dict()
_tracked_pairs = set()
_known_pairs_meta = dict()

PRICE_CACHE = {}                         # key -> (price,ts)
PRICE_TTL   = 60

ATHS = {}                                # token_key -> float (max)
ATH_FILE = os.path.join(DATA_DIR,"aths.json")

_guard_positions = dict()                # token_key -> {entry,qty,peak,ts}
_alert_cooldown  = dict()                # "SYM_scope" -> last ts

EPS = 1e-12

# Ensure data dir
os.makedirs(DATA_DIR, exist_ok=True)

# ----------------------- Utils -----------------------
def now_dt():
    return datetime.now()

def ymd(dt=None):
    return (dt or now_dt()).strftime("%Y-%m-%d")

def month_prefix(dt=None):
    return (dt or now_dt()).strftime("%Y-%m")

def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

def read_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except Exception:
        return default

def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp,"w",encoding="utf-8") as f:
        json.dump(obj,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path)

def send_telegram(text:str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        url = TELEGRAM_URL.format(token=TELEGRAM_BOT_TOKEN)
        payload={"chat_id":TELEGRAM_CHAT_ID,"text":text,"parse_mode":"Markdown"}
        r = SESSION.post(url,data=payload,timeout=12)
        if r.status_code != 200:
            log.warning("Telegram status=%s body=%s", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        log.warning("telegram error: %s", e)
        return False

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
    try: return abs(float(x))>EPS
    except: return False

# ----------------------- ATH persistence -----------------------
def load_aths():
    global ATHS
    ATHS = read_json(ATH_FILE, {})

def save_aths():
    try: write_json(ATH_FILE, ATHS)
    except: pass

# ----------------------- Prices -----------------------
def _best_price_from_pairs(pairs):
    if not pairs: return None
    best=None; best_liq=-1.0
    for p in pairs:
        try:
            chain=str(p.get("chainId","")).lower()
            if chain and chain!="cronos": continue
            liq=float((p.get("liquidity") or {}).get("usd") or 0)
            price=float(p.get("priceUsd") or 0)
            if price<=0: continue
            if liq>best_liq: best=price; best_liq=liq
        except: continue
    return best

def dexs_token_price(token_addr):
    # try /tokens/{chain}/{addr}, then /tokens/{addr}
    d = safe_get(f"{DEX_BASE_TOKENS}/cronos/{token_addr}")
    p = _best_price_from_pairs(d.get("pairs") if isinstance(d,dict) else None)
    if p: return p
    d = safe_get(f"{DEX_BASE_TOKENS}/{token_addr}")
    p = _best_price_from_pairs(d.get("pairs") if isinstance(d,dict) else None)
    return p

def dexs_search_price(q):
    d = safe_get(DEX_BASE_SEARCH, params={"q":q})
    return _best_price_from_pairs(d.get("pairs") if isinstance(d,dict) else None)

def cg_price_contract(addr):
    d = safe_get(f"{CG_PRICE_SIMPLE}/token_price/cronos", params={"contract_addresses":addr,"vs_currencies":"usd"})
    try:
        v = d.get(addr.lower())
        if v and "usd" in v: return float(v["usd"])
    except: pass
    return None

def cg_price_cro():
    d = safe_get(f"{CG_PRICE_SIMPLE}/price", params={"ids":"cronos,crypto-com-chain","vs_currencies":"usd"})
    for k in ("cronos","crypto-com-chain"):
        try:
            v=d.get(k); 
            if v and "usd" in v: return float(v["usd"])
        except: pass
    return None

def get_price_usd(symbol_or_addr):
    key = (symbol_or_addr or "").strip().lower()
    if not key: return 0.0
    now = time.time()
    c = PRICE_CACHE.get(key)
    if c and now-c[1]<PRICE_TTL: return c[0] or 0.0

    price=None
    if key in ("cro","wcro","wrappedcro","wrapped cro","w-cro"):
        price = dexs_search_price("cro usdt") or dexs_search_price("wcro usdt") or cg_price_cro()
    elif key.startswith("0x") and len(key)==42:
        price = dexs_token_price(key) or cg_price_contract(key) or dexs_search_price(key)
    else:
        price = dexs_search_price(key) or dexs_search_price(f"{key} usdt")

    PRICE_CACHE[key]=(price,now)
    if price is None: log.debug("price miss for %s", key)
    return float(price or 0.0)

def get_token_pct_changes_for_symbol(symbol):
    """Return dict like {'change24h': x, 'change2h': y, 'price': p, 'pairUrl': url}"""
    out = {"change24h":None,"change2h":None,"price":None,"pairUrl":None}
    try:
        d = safe_get(DEX_BASE_SEARCH, params={"q":symbol})
        pairs = d.get("pairs") if isinstance(d,dict) else None
        if not pairs: return out
        # pick best by liquidity on Cronos
        best=None; best_liq=-1.0
        for p in pairs:
            if str(p.get("chainId","")).lower()!="cronos": continue
            liq=float((p.get("liquidity") or {}).get("usd") or 0)
            if liq>best_liq: best=p; best_liq=liq
        if not best: return out
        out["price"]= float(best.get("priceUsd") or 0) or None
        ch = best.get("priceChange") or {}
        out["change24h"] = float(ch.get("h24") or 0)
        out["change2h"]  = float(ch.get("h2") or 0) if "h2" in ch else None
        out["pairUrl"]   = f"https://dexscreener.com/cronos/{best.get('pairAddress')}"
        return out
    except Exception as e:
        log.debug("pct change error: %s", e)
        return out

# ----------------------- Etherscan fetchers -----------------------
def fetch_native_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params=dict(chainid=CRONOS_CHAINID,module="account",action="txlist",
                address=WALLET_ADDRESS,startblock=0,endblock=99999999,
                page=1,offset=limit,sort="desc",apikey=ETHERSCAN_API)
    d = safe_get(ETHERSCAN_V2_URL, params=params)
    if isinstance(d,dict) and str(d.get("status",""))=="1" and isinstance(d.get("result"),list):
        return d["result"]
    return []

def fetch_token_txs(limit=50):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params=dict(chainid=CRONOS_CHAINID,module="account",action="tokentx",
                address=WALLET_ADDRESS,startblock=0,endblock=99999999,
                page=1,offset=limit,sort="desc",apikey=ETHERSCAN_API)
    d = safe_get(ETHERSCAN_V2_URL, params=params)
    if isinstance(d,dict) and str(d.get("status",""))=="1" and isinstance(d.get("result"),list):
        return d["result"]
    return []

# ----------------------- Ledger helpers -----------------------
def _append_ledger(entry):
    path = data_file_for_today()
    data = read_json(path, {"date": ymd(),"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    data["entries"].append(entry)
    data["net_usd_flow"]  = float(data.get("net_usd_flow",0.0))  + float(entry.get("usd_value",0.0))
    data["realized_pnl"]  = float(data.get("realized_pnl",0.0))  + float(entry.get("realized_pnl",0.0))
    write_json(path, data)

def _replay_today_cost_basis():
    global _position_qty, _position_cost, _realized_pnl_today
    _position_qty.clear(); _position_cost.clear(); _realized_pnl_today=0.0
    data = read_json(data_file_for_today(), None)
    if not isinstance(data,dict): return
    for e in data.get("entries",[]):
        key = e.get("token_addr") or ("CRO" if e.get("token")=="CRO" else e.get("token"))
        amt = float(e.get("amount") or 0)
        price = float(e.get("price_usd") or 0)
        realized = _update_cost_basis(key, amt, price)
        e["realized_pnl"]=realized
    data["realized_pnl"]=sum(float(x.get("realized_pnl",0)) for x in data.get("entries",[]))
    write_json(data_file_for_today(), data)

# ----------------------- Cost-basis -----------------------
def _update_cost_basis(key, signed_qty, price):
    global _realized_pnl_today
    q = _position_qty[key]; c=_position_cost[key]
    realized=0.0
    if signed_qty>EPS:
        add_cost = signed_qty*(price or 0)
        _position_qty[key]=q+signed_qty
        _position_cost[key]=c+add_cost
    elif signed_qty<-EPS:
        sell = -signed_qty
        if q>EPS:
            avg = c/q if q>EPS else (price or 0)
            used = min(sell, q)
            realized = (price-avg)*used
            _position_qty[key]=q-used
            _position_cost[key]=max(0.0, c-avg*used)
    _realized_pnl_today += realized
    return realized

# ----------------------- Handlers -----------------------
def handle_native_tx(tx):
    h=tx.get("hash")
    if not h or h in _seen_native: return
    _seen_native.add(h)
    val_raw=tx.get("value","0")
    try: amt = int(val_raw)/10**18
    except:
        try: amt=float(val_raw)
        except: amt=0.0
    frm=(tx.get("from") or "").lower(); to=(tx.get("to") or "").lower()
    ts=int(tx.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts) if ts>0 else now_dt()
    sign= +1 if to==WALLET_ADDRESS else (-1 if frm==WALLET_ADDRESS else 0)
    if sign==0 or abs(amt)<=EPS: return
    price = get_price_usd("CRO") or 0.0
    usd   = sign*amt*price

    _token_balances["CRO"] += sign*amt
    _token_meta["CRO"]={"symbol":"CRO","decimals":18}
    realized=_update_cost_basis("CRO", sign*amt, price)

    # ATH check
    _maybe_ath_alert("CRO", price)

    # Telegram
    send_telegram(
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\n"
        f"Hash: {CRONOS_TX.format(txhash=h)}\n"
        f"Time: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amt:.6f} CRO\n"
        f"Price: ${fmt_price(price)}\n"
        f"USD value: ${fmt_amt(usd)}"
    )
    _append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h,
        "type":"native",
        "token":"CRO","token_addr":None,
        "amount": sign*amt,
        "price_usd": price,
        "usd_value": usd,
        "realized_pnl": realized,
        "from": frm, "to": to,
    })

def handle_erc20_tx(t):
    h=t.get("hash"); 
    if not h or h in _seen_erc20: return
    frm=(t.get("from") or "").lower(); to=(t.get("to") or "").lower()
    if WALLET_ADDRESS not in (frm,to): return
    _seen_erc20.add(h)

    token_addr=(t.get("contractAddress") or "").lower()
    symbol=(t.get("tokenSymbol") or token_addr[:8]).upper()
    try: decimals=int(t.get("tokenDecimal") or 18)
    except: decimals=18
    val_raw=t.get("value","0")
    try: amount=int(val_raw)/(10**decimals)
    except:
        try: amount=float(val_raw)
        except: amount=0.0
    ts=int(t.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts) if ts>0 else now_dt()

    sign = +1 if to==WALLET_ADDRESS else -1
    # Contract-first price
    if token_addr and token_addr.startswith("0x") and len(token_addr)==42:
        price = dexs_token_price(token_addr) or get_price_usd(token_addr) or get_price_usd(symbol) or 0.0
    else:
        price = get_price_usd(symbol) or 0.0
    usd = sign*amount*price

    # state
    _token_balances[token_addr]+=sign*amount
    _token_meta[token_addr]={"symbol":symbol,"decimals":decimals}
    realized=_update_cost_basis(token_addr, sign*amount, price)

    # ATH check
    _maybe_ath_alert(token_addr or symbol, price)

    # mini summary & guard hook
    _mini_trade_summary(symbol, token_addr, sign*amount, price)

    # notify
    send_telegram(
        f"*Token TX* ({'IN' if sign>0 else 'OUT'}) {symbol}\n"
        f"Hash: {CRONOS_TX.format(txhash=h)}\n"
        f"Time: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {symbol}\n"
        f"Price: ${fmt_price(price)}\n"
        f"USD value: ${fmt_amt(usd)}"
    )

    _append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h,
        "type":"erc20",
        "token": symbol,
        "token_addr": token_addr,
        "amount": sign*amount,
        "price_usd": price,
        "usd_value": usd,
        "realized_pnl": realized,
        "from": frm, "to": to,
    })

    # auto adopt pair if IN buy
    if sign>0:
        _adopt_token_pairs_for_monitor("cronos", token_addr)

# ----------------------- ATH -----------------------
def _maybe_ath_alert(key, price):
    if not nonzero(price): return
    k = key.lower()
    old = ATHS.get(k)
    if old is None or price>old:
        ATHS[k]=price
        save_aths()
        sym = _token_meta.get(key,{}).get("symbol") if key.startswith("0x") else (key.upper() if key!="CRO" else "CRO")
        send_telegram(f"🏆 New ATH {sym or key}: ${fmt_price(price)}")

# ----------------------- Mini summary & Guard -----------------------
def _mini_trade_summary(symbol, token_addr, qty, live_price):
    key = token_addr or symbol
    avg = (_position_cost[key]/_position_qty[key]) if _position_qty[key]>EPS else live_price
    unreal = 0.0
    if _position_qty[key]>EPS and live_price>0:
        unreal = live_price*_position_qty[key] - _position_cost[key]
    prefix = "BUY" if qty>0 else "SELL"
    send_telegram(
        f"• {prefix} {symbol} {fmt_amt(abs(qty))} @ live ${fmt_price(live_price)}\n"
        f"   Open: {fmt_amt(max(0.0,_position_qty[key]))} {symbol} | Avg: ${fmt_price(avg)} | Unreal: ${fmt_amt(unreal)}"
    )
    # Guard: track last BUY
    if qty>0 and live_price>0:
        _guard_positions[key]={"entry":live_price,"qty":_position_qty[key],"peak":live_price,"ts":time.time()}

# ----------------------- Wallet monitor loop -----------------------
def wallet_monitor_loop():
    log.info("Wallet monitor starting; loading initial recent txs...")
    # seed seen sets
    for tx in fetch_native_txs(limit=50): 
        h=tx.get("hash"); 
        if h: _seen_native.add(h)
    for tt in fetch_token_txs(limit=100):
        h=tt.get("hash"); 
        if h: _seen_erc20.add(h)

    send_telegram(f"🚀 Wallet monitor started for `{WALLET_ADDRESS}` (Cronos).")
    _replay_today_cost_basis()

    while not shutdown_event.is_set():
        try:
            # native
            for tx in reversed(fetch_native_txs(limit=25)):
                handle_native_tx(tx)
            # erc20
            for t in reversed(fetch_token_txs(limit=50)):
                handle_erc20_tx(t)
        except Exception as e:
            log.warning("wallet loop err: %s", e)
        for _ in range(WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Dexscreener monitor & discovery -----------------------
def _slug(chain, pair_address): return f"{chain}/{pair_address}".lower()

def fetch_pair_slug(s):
    d = safe_get(f"{DEX_BASE_PAIRS}/{s}")
    if isinstance(d,dict) and "pair" in d and isinstance(d["pair"],dict): return d["pair"]
    if isinstance(d,dict) and "pairs" in d and isinstance(d["pairs"],list) and d["pairs"]: return d["pairs"][0]
    return None

def fetch_token_pairs(chain, token_addr):
    d = safe_get(f"{DEX_BASE_TOKENS}/{chain}/{token_addr}")
    if isinstance(d,dict) and isinstance(d.get("pairs"),list): return d["pairs"]
    return []

def fetch_search_pairs(q):
    d = safe_get(DEX_BASE_SEARCH, params={"q":q})
    if isinstance(d,dict) and isinstance(d.get("pairs"),list): return d["pairs"]
    return []

def ensure_tracking_pair(chain, pair_addr, meta=None):
    s=_slug(chain,pair_addr)
    if s in _tracked_pairs: return
    _tracked_pairs.add(s); _last_prices[s]=None; _price_history[s]=deque(maxlen=PRICE_WINDOW)
    if meta: _known_pairs_meta[s]=meta
    ds=f"https://dexscreener.com/{chain}/{pair_addr}"
    sym=None
    if isinstance(meta,dict):
        bt=meta.get("baseToken") or {}; sym=bt.get("symbol")
    title=f"{sym} ({s})" if sym else s
    send_telegram(f"🆕 Now monitoring pair: {title}\n{ds}")

def _adopt_token_pairs_for_monitor(chain, token_addr):
    if not token_addr or not token_addr.startswith("0x"): return
    pairs=fetch_token_pairs(chain, token_addr)
    if not pairs: return
    # pick best by liquidity
    best=None; best_liq=-1.0
    for p in pairs:
        liq=float((p.get("liquidity") or {}).get("usd") or 0)
        if liq>best_liq: best=p; best_liq=liq
    if best and best.get("pairAddress"):
        ensure_tracking_pair(chain, best["pairAddress"], best)

def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        log.info("No tracked pairs; monitor waits until discovery/seed adds some.")
    while not shutdown_event.is_set():
        if not _tracked_pairs:
            time.sleep(DEX_POLL); continue
        try:
            for s in list(_tracked_pairs):
                pair = fetch_pair_slug(s)
                if not pair: continue
                price = None
                try: price=float(pair.get("priceUsd") or 0) or None
                except: price=None
                if price and price>0:
                    _price_history[s].append(price)
                    prev=_last_prices.get(s)
                    _last_prices[s]=price
                    # spike detection
                    if len(_price_history[s])>=2:
                        first=_price_history[s][0]; last=_price_history[s][-1]
                        if first>0:
                            pct=(last-first)/first*100.0
                            if abs(pct)>=SPIKE_THRESHOLD:
                                vol_h1=None
                                vol=pair.get("volume") or {}
                                if isinstance(vol,dict):
                                    try: vol_h1=float(vol.get("h1") or 0)
                                    except: vol_h1=None
                                if not (MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1<MIN_VOLUME_FOR_ALERT):
                                    sym=(pair.get("baseToken") or {}).get("symbol") or s
                                    send_telegram(f"🚨 Spike on {sym}: {pct:.2f}%\nPrice: ${fmt_price(price)}")
                                    _price_history[s].clear()
                # new trade
                last_tx=(pair.get("lastTx") or {}).get("hash")
                if last_tx and _last_pair_tx.get(s)!=last_tx:
                    _last_pair_tx[s]=last_tx
                    sym=(pair.get("baseToken") or {}).get("symbol") or s
                    send_telegram(f"🔔 New trade on {sym}\nTx: {CRONOS_TX.format(txhash=last_tx)}")
        except Exception as e:
            log.debug("pairs loop err: %s", e)
        for _ in range(DEX_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

def discovery_loop():
    if not DISCOVER_ENABLED:
        log.info("Discovery disabled."); return
    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos) with filters.")
    while not shutdown_event.is_set():
        try:
            found = fetch_search_pairs(DISCOVER_QUERY) or []
            adopted=0
            now_ms=int(time.time()*1000)
            for p in found:
                if str(p.get("chainId","")).lower()!="cronos": continue
                # Filters
                base=(p.get("baseToken") or {}).get("symbol","").upper()
                quote=(p.get("quoteToken") or {}).get("symbol","").upper()
                if DISCOVER_REQUIRE_WCRO_QUOTE and quote!="WCRO": continue
                if DISCOVER_WHITELIST_BASE and base not in DISCOVER_WHITELIST_BASE: continue
                if base in DISCOVER_BLACKLIST_BASE: continue
                liq=float((p.get("liquidity") or {}).get("usd") or 0)
                vol24=float((p.get("volume") or {}).get("h24") or 0)
                if liq<DISCOVER_MIN_LIQ_USD or vol24<DISCOVER_MIN_VOL24_USD: continue
                ch=p.get("priceChange") or {}
                # έστω 24h abs change
                abspct=abs(float(ch.get("h24") or 0))
                if abspct < DISCOVER_MIN_ABS_CHG_PCT: continue
                created = int(p.get("pairCreatedAt") or 0)
                if created>0:
                    age_h = max(0,(now_ms-created)/3600000.0)
                    if age_h>DISCOVER_MAX_PAIR_AGE_HOURS: continue
                pair_addr=p.get("pairAddress")
                if not pair_addr: continue
                s=_slug("cronos", pair_addr)
                if s in _tracked_pairs: continue
                ensure_tracking_pair("cronos", pair_addr, p)
                adopted+=1
                if adopted>=DISCOVER_LIMIT: break
        except Exception as e:
            log.debug("discovery err: %s", e)
        for _ in range(DISCOVER_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Snapshot / Holdings -----------------------
def get_wallet_balances_snapshot():
    out={}
    if CRONOSCAN_API and WALLET_ADDRESS:
        try:
            d=safe_get("https://api.cronoscan.com/api",
                       params={"module":"account","action":"tokenlist","address":WALLET_ADDRESS,"apikey":CRONOSCAN_API})
            if isinstance(d,dict) and isinstance(d.get("result"),list):
                for t in d["result"]:
                    sym = (t.get("symbol") or t.get("tokenSymbol") or t.get("name") or "").upper()
                    dec = int(t.get("decimals") or t.get("tokenDecimal") or 18)
                    bal_raw=t.get("balance") or t.get("tokenBalance") or "0"
                    try: amt=int(bal_raw)/(10**dec)
                    except:
                        try: amt=float(bal_raw)
                        except: amt=0.0
                    if sym: out[sym]=out.get(sym,0.0)+amt
        except Exception as e:
            log.debug("cronoscan snapshot err: %s", e)
    if not out:
        # fallback runtime
        for k,amt in _token_balances.items():
            if k=="CRO": out["CRO"]=out.get("CRO",0.0)+amt
            else:
                meta=_token_meta.get(k,{})
                sym=(meta.get("symbol") or k[:8]).upper()
                out[sym]=out.get(sym,0.0)+amt
        if "CRO" not in out:
            out["CRO"]=float(_token_balances.get("CRO",0.0))
    return out

def compute_holdings_usd():
    total=0.0; breakdown=[]; unreal=0.0
    # CRO
    cro_amt=max(0.0,_token_balances.get("CRO",0.0))
    if cro_amt>EPS:
        p=get_price_usd("CRO") or 0.0
        val=cro_amt*p; total+=val
        breakdown.append({"token":"CRO","token_addr":None,"amount":cro_amt,"price_usd":p,"usd_value":val})
        q=_position_qty.get("CRO",0.0); c=_position_cost.get("CRO",0.0)
        if q>EPS: unreal+=(val-c)
    # Tokens
    for addr,amt in list(_token_balances.items()):
        if addr=="CRO": continue
        amt=max(0.0,amt)
        if amt<=EPS: continue
        meta=_token_meta.get(addr,{})
        sym=(meta.get("symbol") or addr[:8]).upper()
        p = 0.0
        if isinstance(addr,str) and addr.startswith("0x") and len(addr)==42:
            p = dexs_token_price(addr) or get_price_usd(addr) or get_price_usd(sym) or 0.0
        else:
            p = get_price_usd(sym) or 0.0
        val=amt*p; total+=val
        breakdown.append({"token":sym,"token_addr":addr,"amount":amt,"price_usd":p,"usd_value":val})
        q=_position_qty.get(addr,0.0); c=_position_cost.get(addr,0.0)
        if q>EPS: unreal += (val-c)
    return total, breakdown, unreal

# ----------------------- Month aggregates -----------------------
def sum_month_net_and_real():
    pref = month_prefix()
    flow=0.0; real=0.0
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json") and pref in fn:
                d=read_json(os.path.join(DATA_DIR,fn),None)
                if isinstance(d,dict):
                    flow+=float(d.get("net_usd_flow",0.0))
                    real+=float(d.get("realized_pnl",0.0))
    except: pass
    return flow, real

# ----------------------- Per-asset summarize (today) -----------------------
def summarize_today_per_asset():
    path=data_file_for_today()
    data=read_json(path,{"date":ymd(),"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    per = {}
    for e in data.get("entries",[]):
        tok=e.get("token") or "?"
        addr=e.get("token_addr")
        d=per.setdefault(tok, {"flow":0.0,"real":0.0,"qty_today":0.0,"last_price":None,"addr":None})
        d["flow"] += float(e.get("usd_value") or 0.0)
        d["real"] += float(e.get("realized_pnl") or 0.0)
        d["qty_today"] += float(e.get("amount") or 0.0)
        pu = float(e.get("price_usd") or 0.0)
        if pu>0: d["last_price"]=pu
        if addr and not d["addr"]: d["addr"]=addr
    # live enrich, open qty & unreal
    for tok,rec in per.items():
        addr=rec["addr"]
        if addr:
            open_qty = _position_qty.get(addr,0.0)
            open_cost= _position_cost.get(addr,0.0)
            price = dexs_token_price(addr) or get_price_usd(addr) or 0.0
        else:
            # CRO ή symbol-based
            key = "CRO" if tok.upper()=="CRO" else tok
            open_qty = _position_qty.get(key,0.0)
            open_cost= _position_cost.get(key,0.0)
            price = get_price_usd(key) or 0.0
        if not price and rec["last_price"]:
            price=rec["last_price"]
        unreal = (price*open_qty - open_cost) if (open_qty>EPS and price>0) else 0.0
        rec["open_qty"]=open_qty
        rec["price"]=price
        rec["unreal"]=unreal
    return per

# ----------------------- Report builder -----------------------
def build_day_report_text():
    path=data_file_for_today()
    data=read_json(path,{"date":ymd(),"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    entries=data.get("entries",[])
    lines=[f"*📒 Daily Report* ({data.get('date')})"]
    if entries:
        lines.append("*Transactions:*")
        for e in entries[-20:]:
            tm=e.get("time","")[-8:]; tok=e.get("token"); amt=e.get("amount") or 0.0
            up = e.get("price_usd") or 0.0
            usd= e.get("usd_value") or 0.0
            rp = float(e.get("realized_pnl",0.0) or 0.0)
            pl = f"  PnL: ${fmt_amt(rp)}" if abs(rp)>1e-9 else ""
            lines.append(f"• {tm} — {'IN' if amt>0 else 'OUT'} {tok} {fmt_amt(amt)}  @ ${fmt_price(up)}  (${fmt_amt(usd)}){pl}")
        if len(entries)>20: lines.append(f"_…and {len(entries)-20} earlier txs._")
    else:
        lines.append("_No transactions today._")

    lines.append(f"\nNet USD flow today: ${fmt_amt(data.get('net_usd_flow',0.0))}")
    lines.append(f"Realized PnL today: ${fmt_amt(data.get('realized_pnl',0.0))}")

    tot, br, unr = compute_holdings_usd()
    lines.append(f"Holdings (MTM) now: ${fmt_amt(tot)}")
    if br:
        for b in br[:15]:
            lines.append(f"  – {b['token']}: {fmt_amt(b['amount'])} @ ${fmt_price(b['price_usd'])} = ${fmt_amt(b['usd_value'])}")
        if len(br)>15: lines.append(f"  …and {len(br)-15} more.")
    lines.append(f"Unrealized PnL (open positions): ${fmt_amt(unr)}")

    per = summarize_today_per_asset()
    if per:
        lines.append("\n*Per-Asset Summary (Today):*")
        order = sorted(per.items(), key=lambda kv: abs(kv[1]["flow"]), reverse=True)
        for tok,rec in order[:12]:
            extra = f" | unreal ${fmt_amt(rec['unreal'])}" if rec["open_qty"]>EPS and rec["price"]>0 else ""
            lines.append(
                f"  • {tok}: flow ${fmt_amt(rec['flow'])} | realized ${fmt_amt(rec['real'])} "
                f"| today qty {fmt_amt(rec['qty_today'])} | price ${fmt_price(rec['price'] or 0)}{extra}"
            )
        if len(order)>12: lines.append(f"  …and {len(order)-12} more.")

    mflow, mreal = sum_month_net_and_real()
    lines.append(f"\nMonth Net Flow: ${fmt_amt(mflow)}")
    lines.append(f"Month Realized PnL: ${fmt_amt(mreal)}")
    return "\n".join(lines)

# ----------------------- Intraday & EOD -----------------------
def intraday_report_loop():
    send_telegram("⏱ Intraday reporting enabled.")
    last=time.time()
    while not shutdown_event.is_set():
        if time.time()-last >= INTRADAY_HOURS*3600:
            try: send_telegram("🟡 *Intraday Update*\n"+build_day_report_text())
            except Exception as e: log.warning("intraday err: %s", e)
            last=time.time()
        time.sleep(5)

def end_of_day_scheduler_loop():
    send_telegram(f"🕛 End-of-day scheduler active (at {EOD_HOUR:02d}:{EOD_MINUTE:02d} {TZ}).")
    while not shutdown_event.is_set():
        now=now_dt()
        target=now.replace(hour=EOD_HOUR,minute=EOD_MINUTE,second=0,microsecond=0)
        if now>=target: target+=timedelta(days=1)
        wait=(target-now).total_seconds()
        for _ in range(int(wait//5)+1):
            if shutdown_event.is_set(): break
            time.sleep(5)
        if shutdown_event.is_set(): break
        try: send_telegram("🟢 *End of Day Report*\n"+build_day_report_text())
        except Exception as e: log.warning("eod err: %s", e)

# ----------------------- Reconciliation (basic pairing) -----------------------
def reconcile_swaps_from_entries():
    data=read_json(data_file_for_today(),{"entries":[]})
    es=data.get("entries",[])
    out=[]
    i=0
    while i<len(es):
        e=es[i]
        if float(e.get("amount",0))<0:
            for j in range(i+1, min(i+6,len(es))):
                e2=es[j]
                if float(e2.get("amount",0))>0 and e2.get("token")!=e.get("token"):
                    out.append((e,e2)); break
        i+=1
    return out

# ----------------------- Alerts (24h) -----------------------
def alerts_monitor_loop():
    log.info("Thread alerts_monitor starting.")
    send_telegram(f"🛰 Alerts monitor active every {ALERTS_INTERVAL_MIN}m. Wallet 24h dump/pump: {DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT}.")
    while not shutdown_event.is_set():
        try:
            watch_syms=set()
            # from runtime balances/meta
            for k,v in _token_balances.items():
                if k=="CRO" and v>EPS: watch_syms.add("CRO")
                elif isinstance(k,str) and k.startswith("0x"):
                    sym=_token_meta.get(k,{}).get("symbol")
                    if sym: watch_syms.add(sym)
            # do checks
            for sym in watch_syms:
                ch = get_token_pct_changes_for_symbol(sym)
                if not ch or ch["change24h"] is None: continue
                pct24=ch["change24h"]; price=ch["price"]; url=ch["pairUrl"] or ""
                scope="24h"
                # dump
                if pct24<=DUMP_ALERT_24H_PCT:
                    key=f"{sym}_{scope}_dump"
                    if time.time()-_alert_cooldown.get(key,0) >= ALERTS_INTERVAL_MIN*60:
                        send_telegram(f"📉 Dump Alert {sym} {scope} {pct24:.2f}%\nPrice ${fmt_price(price or 0)}\n{url}")
                        _alert_cooldown[key]=time.time()
                # pump
                if pct24>=PUMP_ALERT_24H_PCT:
                    key=f"{sym}_{scope}_pump"
                    if time.time()-_alert_cooldown.get(key,0) >= ALERTS_INTERVAL_MIN*60:
                        send_telegram(f"🚀 Pump Alert {sym} {scope} {pct24:.2f}%\nPrice ${fmt_price(price or 0)}\n{url}")
                        _alert_cooldown[key]=time.time()
        except Exception as e:
            log.debug("alerts loop err: %s", e)
        for _ in range(ALERTS_INTERVAL_MIN*60//5 or 1):
            if shutdown_event.is_set(): break
            time.sleep(5)

# ----------------------- Guard monitor -----------------------
def guard_monitor_loop():
    log.info("Thread guard_monitor starting.")
    send_telegram(f"🛡 Guard monitor active: {GUARD_WINDOW_MIN}m window, alert below {GUARD_DUMP_FROM_ENTRY:.1f}% / above {GUARD_PUMP_FROM_ENTRY:.1f}% / trailing {GUARD_TRAIL_DROP_FROM_PK:.1f}%.")
    while not shutdown_event.is_set():
        try:
            now=time.time()
            for key,st in list(_guard_positions.items()):
                # expire window
                if now - st.get("ts",now) > GUARD_WINDOW_MIN*60:
                    _guard_positions.pop(key,None); continue
                entry=st.get("entry",0.0); qty=st.get("qty",0.0); peak=st.get("peak",entry)
                if qty<=0 or entry<=0: 
                    _guard_positions.pop(key,None); continue
                # live price
                if isinstance(key,str) and key.startswith("0x") and len(key)==42:
                    lp = dexs_token_price(key) or get_price_usd(key) or 0.0
                else:
                    lp = get_price_usd(key) or 0.0
                if not lp: continue
                if lp>peak: st["peak"]=lp; peak=lp
                change = (lp-entry)/entry*100.0
                trail  = (lp-peak)/peak*100.0 if peak>0 else 0.0
                # pump
                if change>=GUARD_PUMP_FROM_ENTRY:
                    k=f"{key}_guard_pump"
                    if now-_alert_cooldown.get(k,0)>=60:
                        sym = _token_meta.get(key,{}).get("symbol") if key.startswith("0x") else (key.upper() if key!="CRO" else "CRO")
                        send_telegram(f"🟢 GUARD Pump {sym} {change:+.2f}% from entry (${fmt_price(entry)} → ${fmt_price(lp)})")
                        _alert_cooldown[k]=now
                # dump
                if change<=GUARD_DUMP_FROM_ENTRY:
                    k=f"{key}_guard_dump"
                    if now-_alert_cooldown.get(k,0)>=60:
                        sym = _token_meta.get(key,{}).get("symbol") if key.startswith("0x") else (key.upper() if key!="CRO" else "CRO")
                        send_telegram(f"🔴 GUARD Dump {sym} {change:+.2f}% from entry (${fmt_price(entry)} → ${fmt_price(lp)})")
                        _alert_cooldown[k]=now
                # trailing
                if trail<=GUARD_TRAIL_DROP_FROM_PK and peak>entry:
                    k=f"{key}_guard_trail"
                    if now-_alert_cooldown.get(k,0)>=60:
                        sym = _token_meta.get(key,{}).get("symbol") if key.startswith("0x") else (key.upper() if key!="CRO" else "CRO")
                        send_telegram(f"⚠️ GUARD Trailing {sym}: {trail:.2f}% from peak (${fmt_price(peak)} → ${fmt_price(lp)})")
                        _alert_cooldown[k]=now
        except Exception as e:
            log.debug("guard loop err: %s", e)
        for _ in range(60):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------------- Thread helper -----------------------
def run_with_restart(fn, name):
    def runner():
        while not shutdown_event.is_set():
            try:
                log.info("Thread %s starting.", name)
                fn()
                log.info("Thread %s exited.", name)
                break
            except Exception as e:
                log.exception("Thread %s crashed: %s. Restarting in 3s...", name, e)
                for _ in range(3):
                    if shutdown_event.is_set(): break
                    time.sleep(1)
        log.info("Thread %s terminating.", name)
    t=threading.Thread(target=runner,daemon=True,name=name)
    t.start()
    return t

# ----------------------- Entrypoint -----------------------
def main():
    log.info("Starting monitor with config:")
    log.info("WALLET_ADDRESS: %s", WALLET_ADDRESS)
    log.info("TELEGRAM_BOT_TOKEN present: %s", bool(TELEGRAM_BOT_TOKEN))
    log.info("TELEGRAM_CHAT_ID: %s", TELEGRAM_CHAT_ID)
    log.info("ETHERSCAN_API present: %s", bool(ETHERSCAN_API))
    log.info("DEX_PAIRS: ")
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
        pass
    shutdown_event.set()
    # small join
    time.sleep(2)
    log.info("Shutdown complete.")

def _sig(sig,frame):
    log.info("Signal %s -> shutdown", sig); shutdown_event.set()

if __name__=="__main__":
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    main()
