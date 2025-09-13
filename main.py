#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (FULL, with fixes)
Fixes included:
- /watchlist alias + καλύτερο UX στο /watch (help)
- TCRO δεν συγχέεται με CRO στα holdings/daily
- Price guards για CRO/outliers + history sanitation
"""

import os, sys, time, json, threading, logging, signal
from collections import deque, defaultdict
from datetime import datetime, timedelta

from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# external helpers
from utils.http import safe_get, safe_json
from telegram.api import send_telegram
from reports.day_report import build_day_report_text as _compose_day_report
from reports.ledger import append_ledger, update_cost_basis as ledger_update_cost_basis, replay_cost_basis_over_entries
from reports.aggregates import aggregate_per_asset

# ---------- Bootstrap / TZ ----------
load_dotenv()
def _alias_env(src, dst):
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)
_alias_env("ALERTS_INTERVAL_MINUTES", "ALERTS_INTERVAL_MIN")
_alias_env("DISCOVER_REQUIRE_WCRO_QUOTE", "DISCOVER_REQUIRE_WCRO")

def _init_tz(tz_str: str | None):
    tz = tz_str or "Europe/Athens"
    os.environ["TZ"] = tz
    try:
        import time as _t
        if hasattr(_t, "tzset"):
            _t.tzset()
    except Exception:
        pass
    return ZoneInfo(tz)

TZ = os.getenv("TZ", "Europe/Athens")
LOCAL_TZ = _init_tz(TZ)
def now_dt(): return datetime.now(LOCAL_TZ)
def ymd(dt=None): return (dt or now_dt()).strftime("%Y-%m-%d")
def month_prefix(dt=None): return (dt or now_dt()).strftime("%Y-%m")

# ---------- ENV ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API") or ""
CRONOS_RPC_URL     = os.getenv("CRONOS_RPC_URL") or ""

TOKENS             = os.getenv("TOKENS", "")
DEX_PAIRS          = os.getenv("DEX_PAIRS", "")

LOG_SCAN_BLOCKS = int(os.getenv("LOG_SCAN_BLOCKS", "120000"))
LOG_SCAN_CHUNK  = int(os.getenv("LOG_SCAN_CHUNK",  "5000"))
WALLET_POLL     = int(os.getenv("WALLET_POLL", "15"))
DEX_POLL        = int(os.getenv("DEX_POLL", "60"))
PRICE_WINDOW    = int(os.getenv("PRICE_WINDOW","3"))
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD","5"))
SPIKE_THRESHOLD      = float(os.getenv("SPIKE_THRESHOLD","8"))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT","0"))

DISCOVER_ENABLED  = (os.getenv("DISCOVER_ENABLED","true").lower() in ("1","true","yes","on"))
DISCOVER_QUERY    = os.getenv("DISCOVER_QUERY","cronos")
DISCOVER_LIMIT    = int(os.getenv("DISCOVER_LIMIT","10"))
DISCOVER_POLL     = int(os.getenv("DISCOVER_POLL","120"))
DISCOVER_MIN_LIQ_USD        = float(os.getenv("DISCOVER_MIN_LIQ_USD","30000"))
DISCOVER_MIN_VOL24_USD      = float(os.getenv("DISCOVER_MIN_VOL24_USD","5000"))
DISCOVER_MIN_ABS_CHANGE_PCT = float(os.getenv("DISCOVER_MIN_ABS_CHANGE_PCT","10"))
DISCOVER_MAX_PAIR_AGE_HOURS = int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS","24"))
DISCOVER_REQUIRE_WCRO       = (os.getenv("DISCOVER_REQUIRE_WCRO","false").lower() in ("1","true","yes","on"))
DISCOVER_BASE_WHITELIST     = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_WHITELIST","").split(",") if s.strip()]
DISCOVER_BASE_BLACKLIST     = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_BLACKLIST","").split(",") if s.strip()]

INTRADAY_HOURS  = int(os.getenv("INTRADAY_HOURS","3"))
EOD_HOUR        = int(os.getenv("EOD_HOUR","23"))
EOD_MINUTE      = int(os.getenv("EOD_MINUTE","59"))

ALERTS_INTERVAL_MIN = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
DUMP_ALERT_24H_PCT  = float(os.getenv("DUMP_ALERT_24H_PCT","-15"))
PUMP_ALERT_24H_PCT  = float(os.getenv("PUMP_ALERT_24H_PCT","20"))

GUARD_WINDOW_MIN     = int(os.getenv("GUARD_WINDOW_MIN","60"))
GUARD_PUMP_PCT       = float(os.getenv("GUARD_PUMP_PCT","20"))
GUARD_DROP_PCT       = float(os.getenv("GUARD_DROP_PCT","-12"))
GUARD_TRAIL_DROP_PCT = float(os.getenv("GUARD_TRAIL_DROP_PCT","-8"))

RECEIPT_SYMBOLS = set([s.strip().upper() for s in os.getenv("RECEIPT_SYMBOLS","TCRO").split(",") if s.strip()])

# ---------- Constants / Logging ----------
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"
DATA_DIR         = "/app/data"
ATH_PATH         = os.path.join(DATA_DIR, "ath.json")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("wallet-monitor")

# ---------- Runtime ----------
shutdown_event = threading.Event()
_seen_tx_hashes = set()
_last_prices, _price_history, _last_pair_tx = {}, {}, {}
_tracked_pairs, _known_pairs_meta = set(), {}
_TOKEN_EVENT_LRU_MAX, _TOKEN_HASH_LRU_MAX = 4000, 2000
_seen_token_events, _seen_token_hashes = set(), set()
_seen_token_events_q, _seen_token_hashes_q = deque(maxlen=_TOKEN_EVENT_LRU_MAX), deque(maxlen=_TOKEN_HASH_LRU_MAX)

_token_balances = defaultdict(float)   # "CRO" or contract 0x..
_token_meta     = {}                   # key -> {"symbol","decimals"}

_position_qty   = defaultdict(float)   # key (addr or "CRO")
_position_cost  = defaultdict(float)
_realized_pnl_today = 0.0

EPSILON = 1e-12
_last_intraday_sent = 0.0

PRICE_CACHE, PRICE_CACHE_TTL = {}, 60
ATH, _alert_last_sent = {}, {}
COOLDOWN_SEC = 60*30
_guard = {}  # key -> {"entry","peak","start_ts"}

os.makedirs(DATA_DIR, exist_ok=True)

# ---------- Utils ----------
def _format_amount(a):
    try: a=float(a)
    except: return str(a)
    if abs(a)>=1: return f"{a:,.4f}"
    if abs(a)>=0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

def _format_price(p):
    try: p=float(p)
    except: return str(p)
    if p>=1: return f"{p:,.6f}"
    if p>=0.01: return f"{p:.6f}"
    if p>=1e-6: return f"{p:.8f}"
    return f"{p:.10f}"

def _nonzero(v, eps=1e-12):
    try: return abs(float(v))>eps
    except: return False

def read_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except: return default

def write_json(path, obj):
    tmp=path+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(obj,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path)

def data_file_for_today(): return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

# ---------- ATH ----------
def load_ath():
    global ATH
    ATH = read_json(ATH_PATH, default={})
    if not isinstance(ATH, dict): ATH = {}
def save_ath(): write_json(ATH_PATH, ATH)
def update_ath(key: str, live_price: float):
    if not _nonzero(live_price): return
    prev = ATH.get(key)
    if prev is None or live_price > prev + 1e-12:
        ATH[key] = live_price; save_ath()
        send_telegram(f"🏆 New ATH {key}: ${_format_price(live_price)}")

# ---------- Pricing (Dexscreener) ----------
PRICE_ALIASES = {"tcro":"cro"}
_HISTORY_LAST_PRICE = {}

def _pick_best_price(pairs):
    if not pairs: return None
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")).lower()!="cronos": continue
            liq=float((p.get("liquidity") or {}).get("usd") or 0)
            price=float(p.get("priceUsd") or 0)
            if price<=0: continue
            if liq>best_liq: best_liq, best = liq, price
        except: continue
    return best

def _pairs_for_token_addr(addr: str):
    url1=f"{DEX_BASE_TOKENS}/cronos/{addr}"
    data = safe_json(safe_get(url1,timeout=10)) or {}
    pairs = data.get("pairs") or []
    if not pairs:
        url2=f"{DEX_BASE_TOKENS}/{addr}"
        data = safe_json(safe_get(url2,timeout=10)) or {}
        pairs = data.get("pairs") or []
    if not pairs:
        data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": addr}, timeout=10)) or {}
        pairs = data.get("pairs") or []
    return pairs

def _history_price_fallback(query_key: str, symbol_hint: str=None):
    if not query_key: return None
    k=query_key.strip()
    if not k: return None
    if k.startswith("0x"):
        p=_HISTORY_LAST_PRICE.get(k)
        if p and p>0: return p
    sym=(symbol_hint or k)
    sym=(PRICE_ALIASES.get(sym.lower(), sym.lower())).upper()
    p=_HISTORY_LAST_PRICE.get(sym)
    if p and p>0: return p
    if sym=="CRO":
        p=_HISTORY_LAST_PRICE.get("CRO")
        if p and p>0: return p
    return None

def _price_cro_fallback():
    for q in ["wcro usdc","cro usdc","cro busd","cro dai"]:
        try:
            data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
            p=_pick_best_price(data.get("pairs"))
            if p and p>0: return p
        except: pass
    return None

# ---- price sanity guards (fix) ----
def _is_sane_price(sym_key: str, price: float) -> bool:
    try:
        if price is None or float(price) <= 0:
            return False
        s = (sym_key or "").lower()
        if s in ("cro","wcro","w-cro","wrappedcro","wrapped cro"):
            return 0.01 <= float(price) <= 10.0
        return float(price) > 0
    except Exception:
        return False

def get_price_usd(symbol_or_addr: str):
    if not symbol_or_addr: return None
    key = PRICE_ALIASES.get(symbol_or_addr.strip().lower(), symbol_or_addr.strip().lower())
    now = time.time()
    c = PRICE_CACHE.get(key)
    if c and (now - c[1] < PRICE_CACHE_TTL) and _is_sane_price(key, c[0]): 
        return c[0]

    price=None
    try:
        if key in ("cro","wcro","w-cro","wrappedcro","wrapped cro"):
            for q in ["wcro usdt","cro usdt"]:
                data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
                price=_pick_best_price(data.get("pairs"))
                if price: break
            if not price: price=_price_cro_fallback()
        elif key.startswith("0x") and len(key)==42:
            price=_pick_best_price(_pairs_for_token_addr(key))
        else:
            for q in [key, f"{key} usdt", f"{key} wcro"]:
                data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
                price=_pick_best_price(data.get("pairs"))
                if price: break
    except: price=None

    if (price is None) or (not _is_sane_price(key, price)):
        hist=_history_price_fallback(symbol_or_addr, symbol_hint=symbol_or_addr)
        if hist and _is_sane_price(key, hist):
            price=float(hist)
        else:
            cached = PRICE_CACHE.get(key)
            if cached and _is_sane_price(key, cached[0]):
                price = cached[0]
            else:
                price = None

    if _is_sane_price(key, price):
        PRICE_CACHE[key]=(price, now)
    return price

# ---------- Etherscan ----------
def fetch_latest_wallet_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"txlist",
            "address":WALLET_ADDRESS,"startblock":0,"endblock":99999999,
            "page":1,"offset":limit,"sort":"desc","apikey":ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

def fetch_latest_token_txs(limit=50):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"tokentx",
            "address":WALLET_ADDRESS,"startblock":0,"endblock":99999999,
            "page":1,"offset":limit,"sort":"desc","apikey":ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

# ---------- Cost-basis replay (today) ----------
def _replay_today_cost_basis():
    global _position_qty, _position_cost, _realized_pnl_today
    _position_qty.clear(); _position_cost.clear(); _realized_pnl_today=0.0
    path=data_file_for_today()
    data=read_json(path, default={"date": ymd(),"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    total_realized=replay_cost_basis_over_entries(_position_qty,_position_cost,data.get("entries",[]),eps=EPSILON)
    _realized_pnl_today=float(total_realized)
    data["realized_pnl"]=float(total_realized); write_json(path,data)

# ---------- History maps ----------
def _build_history_maps():
    symbol_to_contract, symbol_conflict = {}, set()
    files=[]
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"): files.append(fn)
    except Exception as ex: log.exception("listdir data error: %s", ex)
    files.sort()
    for fn in files:
        data=read_json(os.path.join(DATA_DIR,fn), default=None)
        if not isinstance(data,dict): continue
        for e in data.get("entries",[]):
            sym=(e.get("token") or "").strip()
            addr=(e.get("token_addr") or "").strip().lower()
            p=float(e.get("price_usd") or 0.0)
            # sanitize absurd history prices (fix)
            def _ok_hist(sym_str, px):
                if px<=0 or px>1e6: return False
                s=(sym_str or "").lower()
                if s in ("cro","wcro"): 
                    return 0.001 <= px <= 100.0
                return True
            if _ok_hist(sym, p):
                if addr and addr.startswith("0x"): _HISTORY_LAST_PRICE[addr]=p
                if sym: _HISTORY_LAST_PRICE[sym.upper()]=p
            if sym and addr and addr.startswith("0x"):
                if sym in symbol_to_contract and symbol_to_contract[sym]!=addr:
                    symbol_conflict.add(sym)
                else:
                    symbol_to_contract.setdefault(sym, addr)
    for s in symbol_conflict: symbol_to_contract.pop(s,None)
    return symbol_to_contract

# ---------- RPC (minimal) ----------
WEB3=None
ERC20_ABI_MIN=[
    {"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":True,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
]
TRANSFER_TOPIC0="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

def _to_checksum(addr:str):
    try:
        from web3 import Web3
        return Web3.to_checksum_address(addr)
    except: return addr

def rpc_init():
    global WEB3
    if not CRONOS_RPC_URL:
        log.warning("CRONOS_RPC_URL not set; RPC disabled."); return False
    try:
        from web3 import Web3
        WEB3=Web3(Web3.HTTPProvider(CRONOS_RPC_URL, request_kwargs={"timeout":15}))
        ok=WEB3.is_connected()
        if not ok: log.warning("Web3 not connected.")
        return ok
    except Exception as e:
        log.exception("web3 init error: %s", e); return False

def rpc_block_number():
    try: return WEB3.eth.block_number if WEB3 else None
    except: return None

def rpc_get_native_balance(addr:str):
    try:
        wei=WEB3.eth.get_balance(addr)
        return float(wei)/(10**18)
    except: return 0.0

_rpc_sym_cache,_rpc_dec_cache={},{}
def rpc_get_symbol_decimals(contract:str):
    if contract in _rpc_sym_cache and contract in _rpc_dec_cache:
        return _rpc_sym_cache[contract], _rpc_dec_cache[contract]
    try:
        c=WEB3.eth.contract(address=_to_checksum(contract), abi=ERC20_ABI_MIN)
        sym=c.functions.symbol().call(); dec=int(c.functions.decimals().call())
        _rpc_sym_cache[contract]=sym; _rpc_dec_cache[contract]=dec
        return sym,dec
    except:
        _rpc_sym_cache[contract]=contract[:8].upper(); _rpc_dec_cache[contract]=18
        return _rpc_sym_cache[contract], _rpc_dec_cache[contract]

def rpc_get_erc20_balance(contract:str, owner:str):
    try:
        c=WEB3.eth.contract(address=_to_checksum(contract), abi=ERC20_ABI_MIN)
        bal=c.functions.balanceOf(_to_checksum(owner)).call()
        _,dec=rpc_get_symbol_decimals(contract)
        return float(bal)/(10**dec)
    except: return 0.0

def rpc_discover_token_contracts_by_logs(owner:str, blocks_back:int, chunk:int):
    if not WEB3: return set()
    latest=rpc_block_number()
    if not latest: return set()
    start=max(1, latest - max(1, blocks_back))
    found=set()
    try:
        wallet_topic="0x"+"0"*24+owner.lower().replace("0x","")
        frm=start
        while frm<=latest:
            to=min(latest, frm+chunk-1)
            for topics in [[TRANSFER_TOPIC0, wallet_topic],[TRANSFER_TOPIC0,None,wallet_topic]]:
                try:
                    logs=WEB3.eth.get_logs({"fromBlock":frm,"toBlock":to,"topics":topics})
                    for lg in logs:
                        addr=(lg.get("address") or "").lower()
                        if addr.startswith("0x"): found.add(addr)
                except: pass
            frm=to+1
    except Exception as e:
        log.debug("rpc_discover_token_contracts_by_logs err: %s", e)
    return found

def rpc_discover_wallet_tokens(window_blocks:int=None, chunk:int=None):
    window_blocks=window_blocks or LOG_SCAN_BLOCKS
    chunk=chunk or LOG_SCAN_CHUNK
    if not rpc_init():
        log.warning("rpc_discover_wallet_tokens: RPC not connected."); return 0
    contracts=set()
    try:
        head=rpc_block_number()
        if head is None: raise RuntimeError("no block number")
        start=max(0, head-window_blocks)
        wallet_cs=_to_checksum(WALLET_ADDRESS)
        topic_wallet="0x"+"0"*24+wallet_cs.lower().replace("0x","")
        def _scan(from_topic,to_topic):
            nonlocal contracts
            frm=start
            while frm<=head:
                to=min(head, frm+chunk-1)
                try:
                    logs=WEB3.eth.get_logs({"fromBlock":frm,"toBlock":to,"topics":[TRANSFER_TOPIC0,from_topic,to_topic]})
                    for lg in logs:
                        addr=(lg.get("address") or "").lower()
                        if addr.startswith("0x"): contracts.add(addr)
                except Exception as e:
                    log.debug("get_logs %s-%s: %s", frm,to,e); time.sleep(0.2)
                frm=to+1
        _scan(topic_wallet,None)
        _scan(None,topic_wallet)
    except Exception as e:
        log.warning("rpc_discover_wallet_tokens (RPC phase) failed: %s", e)

    if not contracts:
        try:
            for t in fetch_latest_token_txs(limit=1000) or []:
                ca=(t.get("contractAddress") or "").lower()
                if ca.startswith("0x"): contracts.add(ca)
            if contracts: log.info("Etherscan fallback discovered %s token contracts.", len(contracts))
        except Exception as e:
            log.warning("Etherscan fallback failed: %s", e)

    if not contracts:
        log.info("rpc_discover_wallet_tokens: no contracts discovered."); return 0

    found_positive=0
    for addr in sorted(contracts):
        try:
            sym,dec=rpc_get_symbol_decimals(addr)
            bal=rpc_get_erc20_balance(addr, WALLET_ADDRESS)
            if bal>EPSILON:
                _token_balances[addr]=bal
                _token_meta[addr]={"symbol":sym or addr[:8].upper(),"decimals":dec or 18}
                found_positive+=1
        except Exception as e:
            log.debug("discover balance/meta error %s: %s", addr, e)
            continue
    log.info("rpc_discover_wallet_tokens: positive-balance tokens discovered: %s", found_positive)
    return found_positive
# ---------- Holdings (RPC / History / Merge) ----------
def gather_all_known_token_contracts():
    known=set()
    for k in list(_token_meta.keys()):
        if isinstance(k,str) and k.startswith("0x"): known.add(k.lower())
    symbol_to_contract=_build_history_maps()
    for addr in symbol_to_contract.values():
        if addr and addr.startswith("0x"): known.add(addr.lower())
    try:
        for t in fetch_latest_token_txs(limit=100) or []:
            addr=(t.get("contractAddress") or "").lower()
            if addr.startswith("0x"): known.add(addr)
    except: pass
    try:
        if rpc_init():
            rpc_found=rpc_discover_token_contracts_by_logs(WALLET_ADDRESS, LOG_SCAN_BLOCKS, LOG_SCAN_CHUNK)
            known |= set(rpc_found or [])
    except: pass
    for item in [x.strip().lower() for x in TOKENS.split(",") if x.strip()]:
        if item.startswith("cronos/"):
            _,addr=item.split("/",1)
            if addr.startswith("0x"): known.add(addr)
    return known

def compute_holdings_usd_via_rpc():
    total, breakdown, unrealized = 0.0, [], 0.0
    _=_build_history_maps()
    cro_amt=0.0
    if rpc_init():
        try: cro_amt=rpc_get_native_balance(WALLET_ADDRESS)
        except: cro_amt=0.0
    if cro_amt>EPSILON:
        cro_price=get_price_usd("CRO") or 0.0
        cro_val=cro_amt*cro_price; total+=cro_val
        breakdown.append({"token":"CRO","token_addr":None,"amount":cro_amt,"price_usd":cro_price,"usd_value":cro_val})
        rem_qty=_position_qty.get("CRO",0.0); rem_cost=_position_cost.get("CRO",0.0)
        if rem_qty>EPSILON and _nonzero(cro_price): unrealized += (cro_amt*cro_price - rem_cost)
    contracts=gather_all_known_token_contracts()
    for addr in sorted(list(contracts)):
        try:
            bal=rpc_get_erc20_balance(addr, WALLET_ADDRESS)
            if bal<=EPSILON: continue
            sym,dec=rpc_get_symbol_decimals(addr)
            pr=get_price_usd(addr) or 0.0
            val=bal*pr; total+=val
            breakdown.append({"token":sym,"token_addr":addr,"amount":bal,"price_usd":pr,"usd_value":val})
            rem_qty=_position_qty.get(addr,0.0); rem_cost=_position_cost.get(addr,0.0)
            if rem_qty>EPSILON and _nonzero(pr): unrealized += (bal*pr - rem_cost)
        except: continue
    breakdown.sort(key=lambda b: float(b.get("usd_value",0.0)), reverse=True)
    return total, breakdown, unrealized

def rebuild_open_positions_from_history():
    pos_qty, pos_cost = defaultdict(float), defaultdict(float)
    symbol_to_contract=_build_history_maps()

    def _update(pos_qty,pos_cost,token_key,signed_amount,price_usd):
        qty=pos_qty[token_key]; cost=pos_cost[token_key]
        if signed_amount>EPSILON:
            pos_qty[token_key]=qty+signed_amount
            pos_cost[token_key]=cost+signed_amount*(price_usd or 0.0)
        elif signed_amount<-EPSILON and qty>EPSILON:
            sell_qty=min(-signed_amount, qty)
            avg_cost=(cost/qty) if qty>EPSILON else (price_usd or 0.0)
            pos_qty[token_key]=qty-sell_qty
            pos_cost[token_key]=max(0.0, cost - avg_cost*sell_qty)

    files=[]
    try:
        for fn in os.listdir(DATA_DIR):
            if fn.startswith("transactions_") and fn.endswith(".json"): files.append(fn)
    except Exception as ex: log.exception("listdir data error: %s", ex)
    files.sort()

    for fn in files:
        data=read_json(os.path.join(DATA_DIR,fn), default=None)
        if not isinstance(data,dict): continue
        for e in data.get("entries",[]):
            sym_raw=(e.get("token") or "").strip()
            addr_raw=(e.get("token_addr") or "").strip().lower()
            amt=float(e.get("amount") or 0.0)
            pr=float(e.get("price_usd") or 0.0)
            symU=sym_raw.upper() if sym_raw else sym_raw
            if addr_raw and addr_raw.startswith("0x"): key=addr_raw
            else:
                mapped=symbol_to_contract.get(sym_raw) or symbol_to_contract.get(symU)
                key=mapped if (mapped and mapped.startswith("0x")) else (symU or sym_raw or "?")
            _update(pos_qty,pos_cost,key,amt,pr)

    for k,v in list(pos_qty.items()):
        if abs(v)<1e-10: pos_qty[k]=0.0
    return pos_qty, pos_cost

def compute_holdings_usd_from_history_positions():
    pos_qty,pos_cost=rebuild_open_positions_from_history()
    total, breakdown, unrealized = 0.0, [], 0.0

    def _sym_for_key(key):
        if isinstance(key,str) and key.startswith("0x"):
            return _token_meta.get(key,{}).get("symbol") or key[:8].upper()
        return str(key)

    def _price_for(key, sym_hint):
        if isinstance(key,str) and key.startswith("0x"):
            p=get_price_usd(key)
        else:
            sym_l=PRICE_ALIASES.get(sym_hint.lower(), sym_hint.lower())
            p=get_price_usd(sym_l)
        if (p is None) or (not p) or (float(p)<=0):
            p=_history_price_fallback(key if (isinstance(key,str) and key.startswith("0x")) else sym_hint, symbol_hint=sym_hint) or 0.0
        return float(p or 0.0)

    for key,amt in pos_qty.items():
        amt=max(0.0,float(amt))
        if amt<=EPSILON: continue
        sym=_sym_for_key(key)
        p=_price_for(key, sym); v=amt*p
        total+=v
        breakdown.append({"token":sym,"token_addr": key if (isinstance(key,str) and key.startswith("0x")) else None,
                          "amount":amt,"price_usd":p,"usd_value":v})
        cost=pos_cost.get(key,0.0)
        if amt>EPSILON and _nonzero(p): unrealized += (amt*p - cost)

    # FIX: κρατάμε καθαρά τα σύμβολα — μόνο explicit CRO normalizes, TCRO μένει TCRO
    for b in breakdown:
        if b["token"].upper() == "CRO":
            b["token"] = "CRO"

    breakdown.sort(key=lambda b: float(b.get("usd_value",0.0)), reverse=True)
    return total, breakdown, unrealized

def compute_holdings_merged():
    total_r, br_r, unrl_r = compute_holdings_usd_via_rpc()
    total_h, br_h, unrl_h = compute_holdings_usd_from_history_positions()

    def _key(b):
        addr=b.get("token_addr"); sym=(b.get("token") or "").upper()
        return addr.lower() if (isinstance(addr,str) and addr.startswith("0x")) else sym

    merged={}
    def _add(b):
        k=_key(b)
        if not k: return
        cur=merged.get(k, {"token":b["token"],"token_addr":b.get("token_addr"),
                           "amount":0.0,"price_usd":0.0,"usd_value":0.0})
        cur["token"]=b["token"] or cur["token"]
        cur["token_addr"]=b.get("token_addr", cur.get("token_addr"))
        cur["amount"] += float(b.get("amount") or 0.0)
        pr=float(b.get("price_usd") or 0.0)
        if pr>0: cur["price_usd"]=pr
        cur["usd_value"]=cur["amount"]*(cur["price_usd"] or 0.0)
        merged[k]=cur

    for b in br_h or []: _add(b)
    for b in br_r or []: _add(b)

    receipts, breakdown, total = [], [], 0.0
    for rec in merged.values():
        symU=(rec["token"] or "").upper()
        if symU in RECEIPT_SYMBOLS or symU=="TCRO":
            receipts.append(rec); continue
        if symU=="CRO": rec["token"]="CRO"
        rec["usd_value"]=float(rec.get("amount",0.0))*float(rec.get("price_usd",0.0) or 0.0)
    # υπολόγισε σύνολο μόνο από breakdown (χωρίς receipts)
    for rec in merged.values():
        symU=(rec["token"] or "").upper()
        if symU in RECEIPT_SYMBOLS or symU=="TCRO": 
            continue
        total += float(rec.get("usd_value",0.0))
        breakdown.append(rec)

    breakdown.sort(key=lambda b: float(b.get("usd_value",0.0)), reverse=True)
    unrealized = unrl_r + unrl_h
    return total, breakdown, unrealized, receipts

# ---------- Day report wrapper ----------
def build_day_report_text():
    date_str=ymd()
    path=data_file_for_today()
    data=read_json(path, default={"date":date_str,"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    entries=data.get("entries",[])
    net_flow=float(data.get("net_usd_flow",0.0))
    realized_today_total=float(data.get("realized_pnl",0.0))
    holdings_total, breakdown, unrealized, _receipts = compute_holdings_merged()
    if not breakdown:
        holdings_total, breakdown, unrealized = compute_holdings_usd_from_history_positions()
    return _compose_day_report(
        date_str=date_str, entries=entries, net_flow=net_flow,
        realized_today_total=realized_today_total, holdings_total=holdings_total,
        breakdown=breakdown, unrealized=unrealized, data_dir=DATA_DIR
)

# ---------- Mini summaries & TX handlers ----------
def _mini_summary_line(token_key, symbol_shown):
    open_qty=_position_qty.get(token_key,0.0)
    open_cost=_position_cost.get(token_key,0.0)
    if token_key=="CRO": live=get_price_usd("CRO") or 0.0
    elif isinstance(token_key,str) and token_key.startswith("0x"): live=get_price_usd(token_key) or 0.0
    else: live=get_price_usd(symbol_shown) or 0.0
    unreal=0.0
    if open_qty>EPSILON and _nonzero(live):
        unreal=open_qty*live - open_cost
    send_telegram(
        f"• {'Open' if open_qty>0 else 'Flat'} {symbol_shown} {_format_amount(open_qty)} @ live ${_format_price(live)}\n"
        f"   Avg: ${_format_price((open_cost/open_qty) if open_qty>EPSILON else 0)} | Unreal: ${_format_amount(unreal)}"
    )

def handle_native_tx(tx: dict):
    h=tx.get("hash")
    if not h or h in _seen_tx_hashes: return
    _seen_tx_hashes.add(h)
    val_raw=tx.get("value","0")
    try: amount_cro=int(val_raw)/(10**18)
    except:
        try: amount_cro=float(val_raw)
        except: amount_cro=0.0
    frm=(tx.get("from") or "").lower()
    to =(tx.get("to") or "").lower()
    ts=int(tx.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts, LOCAL_TZ) if ts>0 else now_dt()
    sign= +1 if to==WALLET_ADDRESS else (-1 if frm==WALLET_ADDRESS else 0)
    if sign==0 or abs(amount_cro)<=EPSILON: return
    price=get_price_usd("CRO") or 0.0
    usd_value=sign*amount_cro*(price or 0.0)

    _token_balances["CRO"]+=sign*amount_cro
    _token_meta["CRO"]={"symbol":"CRO","decimals":18}
    realized=ledger_update_cost_basis(_position_qty,_position_cost,"CRO",sign*amount_cro,price,eps=EPSILON)

    link=CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\n"
        f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount_cro:.6f} CRO\nPrice: ${_format_price(price)}\nUSD value: ${_format_amount(usd_value)}"
    )
    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"), "txhash": h, "type":"native",
        "token":"CRO", "token_addr": None, "amount": sign*amount_cro,
        "price_usd": price, "usd_value": usd_value, "realized_pnl": realized,
        "from": frm, "to": to
    })

def _remember_token_event(key_tuple):
    if key_tuple in _seen_token_events: return False
    _seen_token_events.add(key_tuple); _seen_token_events_q.append(key_tuple)
    if len(_seen_token_events_q)==_TOKEN_EVENT_LRU_MAX:
        while len(_seen_token_events)>_TOKEN_EVENT_LRU_MAX:
            old=_seen_token_events_q.popleft()
            if old in _seen_token_events: _seen_token_events.remove(old)
    return True

def _remember_token_hash(h):
    if not h: return True
    if h in _seen_token_hashes: return False
    _seen_token_hashes.add(h); _seen_token_hashes_q.append(h)
    if len(_seen_token_hashes_q)==_TOKEN_HASH_LRU_MAX:
        while len(_seen_token_hashes)>_TOKEN_HASH_LRU_MAX:
            old=_seen_token_hashes_q.popleft()
            if old in _seen_token_hashes: _seen_token_hashes.remove(old)
    return True

def handle_erc20_tx(t: dict):
    h=t.get("hash") or ""
    frm=(t.get("from") or "").lower()
    to =(t.get("to") or "").lower()
    token_addr=(t.get("contractAddress") or "").lower()
    symbol=t.get("tokenSymbol") or (token_addr[:8] if token_addr else "?")
    try: decimals=int(t.get("tokenDecimal") or 18)
    except: decimals=18
    val_raw=t.get("value","0")

    event_key=(h,token_addr,frm,to,str(val_raw),str(decimals))
    if not _remember_token_event(event_key): return
    _remember_token_hash(h)
    if WALLET_ADDRESS not in (frm,to): return

    try: amount=int(val_raw)/(10**decimals)
    except:
        try: amount=float(val_raw)
        except: amount=0.0
    ts=int(t.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts, LOCAL_TZ) if ts>0 else now_dt()
    sign= +1 if to==WALLET_ADDRESS else -1

    if token_addr and token_addr.startswith("0x") and len(token_addr)==42:
        price=get_price_usd(token_addr) or 0.0
    else:
        price=get_price_usd(symbol) or 0.0
    usd_value=sign*amount*(price or 0.0)

    key=token_addr if token_addr else symbol
    _token_balances[key]+=sign*amount
    if abs(_token_balances[key])<1e-10: _token_balances[key]=0.0
    _token_meta[key]={"symbol":symbol,"decimals":decimals}

    realized=ledger_update_cost_basis(_position_qty,_position_cost,key,sign*amount,(price or 0.0),eps=EPSILON)
    try:
        if _nonzero(price):
            ath_key=token_addr if token_addr else symbol
            update_ath(ath_key, price)
    except: pass

    link=CRONOS_TX.format(txhash=h); direction="IN" if sign>0 else "OUT"
    send_telegram(
        f"Token TX ({direction}) {symbol}\nHash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {symbol}\nPrice: ${_format_price(price)}\nUSD value: ${_format_amount(usd_value)}"
    )
    send_telegram(f"• {'BUY' if sign>0 else 'SELL'} {symbol} {_format_amount(abs(amount))} @ live ${_format_price(price)}")
    _mini_summary_line(key, symbol)
    if sign>0 and _nonzero(price):
        _guard[key]={"entry":float(price),"peak":float(price),"start_ts":time.time()}

    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "txhash": h or None, "type":"erc20", "token": symbol, "token_addr": token_addr or None,
        "amount": sign*amount, "price_usd": price or 0.0, "usd_value": usd_value,
        "realized_pnl": realized, "from": frm, "to": to
    })

# ---------- Dex monitor & discovery ----------
def slug(chain: str, pair_address: str) -> str: return f"{chain}/{pair_address}".lower()

def fetch_pair(slg_str: str):
    return safe_json(safe_get(f"{DEX_BASE_PAIRS}/{slg_str}", timeout=12))

def fetch_token_pairs(chain: str, token_address: str):
    data=safe_json(safe_get(f"{DEX_BASE_TOKENS}/{chain}/{token_address}", timeout=12)) or {}
    return data.get("pairs") or []

def fetch_search(query: str):
    data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=15)) or {}
    return data.get("pairs") or []

def ensure_tracking_pair(chain: str, pair_address: str, meta: dict=None):
    s=slug(chain, pair_address)
    if s in _tracked_pairs: return
    _tracked_pairs.add(s); _last_prices[s]=None; _last_pair_tx[s]=None
    _price_history[s]=deque(maxlen=PRICE_WINDOW)
    if meta: _known_pairs_meta[s]=meta
    ds_link=f"https://dexscreener.com/{chain}/{pair_address}"
    sym=None
    if isinstance(meta,dict):
        bt=meta.get("baseToken") or {}; sym=bt.get("symbol")
    title=f"{sym} ({s})" if sym else s
    send_telegram(f"🆕 Now monitoring pair: {title}\n{ds_link}")

def update_price_history(slg, price):
    hist=_price_history.get(slg) or deque(maxlen=PRICE_WINDOW)
    _price_history[slg]=hist; hist.append(price); _last_prices[slg]=price

def detect_spike(slg):
    hist=_price_history.get(slg)
    if not hist or len(hist)<2: return None
    first, last = hist[0], hist[-1]
    if not first: return None
    pct=(last-first)/first*100.0
    return pct if abs(pct)>=SPIKE_THRESHOLD else None

_last_pair_alert={}
PAIR_ALERT_COOLDOWN=60*10
def _pair_cooldown_ok(key):
    last=_last_pair_alert.get(key,0.0); now=time.time()
    if now-last>=PAIR_ALERT_COOLDOWN:
        _last_pair_alert[key]=now; return True
    return False

def monitor_tracked_pairs_loop():
    if not _tracked_pairs:
        log.info("No tracked pairs; monitor waits.")
    else:
        send_telegram(f"🚀 Dex monitor started: {', '.join(sorted(_tracked_pairs))}")
    while not shutdown_event.is_set():
        if not _tracked_pairs:
            time.sleep(DEX_POLL); continue
        for s in list(_tracked_pairs):
            try:
                data=fetch_pair(s); 
                if not data: continue
                pair=None
                if isinstance(data.get("pair"),dict): pair=data["pair"]
                elif isinstance(data.get("pairs"),list) and data["pairs"]: pair=data["pairs"][0]
                if not pair: continue
                try: price_val=float(pair.get("priceUsd") or 0)
                except: price_val=None
                if price_val and price_val>0:
                    update_price_history(s, price_val)
                    spike_pct=detect_spike(s)
                    if spike_pct is not None:
                        try: vol_h1=float((pair.get("volume") or {}).get("h1") or 0)
                        except: vol_h1=None
                        if not (MIN_VOLUME_FOR_ALERT and vol_h1 and vol_h1<MIN_VOLUME_FOR_ALERT):
                            bt=pair.get("baseToken") or {}; symbol=bt.get("symbol") or s
                            if _pair_cooldown_ok(f"spike:{s}"):
                                send_telegram(f"🚨 Spike on {symbol}: {spike_pct:.2f}%\nPrice: ${_format_price(price_val)}")
                                _price_history[s].clear(); _last_prices[s]=price_val
                prev=_last_prices.get(s)
                if prev and price_val and prev>0:
                    delta=(price_val-prev)/prev*100.0
                    if abs(delta)>=PRICE_MOVE_THRESHOLD and _pair_cooldown_ok(f"move:{s}"):
                        bt=pair.get("baseToken") or {}; symbol=bt.get("symbol") or s
                        send_telegram(f"📈 Price move on {symbol}: {delta:.2f}%\nPrice: ${_format_price(price_val)} (prev ${_format_price(prev)})")
                        _last_prices[s]=price_val
                last_tx=(pair.get("lastTx") or {}).get("hash")
                if last_tx:
                    prev_tx=_last_pair_tx.get(s)
                    if prev_tx!=last_tx and _pair_cooldown_ok(f"trade:{s}"):
                        _last_pair_tx[s]=last_tx
                        bt=pair.get("baseToken") or {}; symbol=bt.get("symbol") or s
                        send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx)}")
            except Exception as e:
                log.debug("pairs loop error %s: %s", s, e)
        for _ in range(DEX_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

def _pair_passes_filters(p):
    try:
        if str(p.get("chainId","")).lower()!="cronos": return False
        bt=p.get("baseToken") or {}; qt=p.get("quoteToken") or {}
        base_sym=(bt.get("symbol") or "").upper(); quote_sym=(qt.get("symbol") or "").upper()
        if DISCOVER_REQUIRE_WCRO and quote_sym!="WCRO": return False
        if DISCOVER_BASE_WHITELIST and base_sym not in DISCOVER_BASE_WHITELIST: return False
        if DISCOVER_BASE_BLACKLIST and base_sym in DISCOVER_BASE_BLACKLIST: return False
        liq=float((p.get("liquidity") or {}).get("usd") or 0)
        if liq<DISCOVER_MIN_LIQ_USD: return False
        vol24=float((p.get("volume") or {}).get("h24") or 0)
        if vol24<DISCOVER_MIN_VOL24_USD: return False
        ch=p.get("priceChange") or {}; best_change=0.0
        for k in ("h1","h4","h6","h24"):
            if k in ch:
                try: best_change=max(best_change, abs(float(ch[k])))
                except: pass
        if best_change<DISCOVER_MIN_ABS_CHANGE_PCT: return False
        created_ms=p.get("pairCreatedAt")
        if created_ms:
            age_h=(time.time()*1000 - float(created_ms))/1000/3600.0
            if age_h>DISCOVER_MAX_PAIR_AGE_HOURS: return False
        return True
    except: return False

def discovery_loop():
    seeds=[p.strip().lower() for p in (DEX_PAIRS or "").split(",") if p.strip()]
    for s in seeds:
        if s.startswith("cronos/"): ensure_tracking_pair("cronos", s.split("/",1)[1])
    token_items=[t.strip().lower() for t in (TOKENS or "").split(",") if t.strip()]
    for t in token_items:
        if not t.startswith("cronos/"): continue
        _,token_addr=t.split("/",1)
        pairs=fetch_token_pairs("cronos", token_addr)
        if pairs:
            p=pairs[0]; pair_addr=p.get("pairAddress")
            if pair_addr: ensure_tracking_pair("cronos", pair_addr, meta=p)
    if not DISCOVER_ENABLED:
        log.info("Discovery disabled."); return
    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos).")
    while not shutdown_event.is_set():
        try:
            found=fetch_search(DISCOVER_QUERY); adopted=0
            for p in found or []:
                if not _pair_passes_filters(p): continue
                pair_addr=p.get("pairAddress")
                if not pair_addr: continue
                s=slug("cronos", pair_addr)
                if s in _tracked_pairs: continue
                ensure_tracking_pair("cronos", pair_addr, meta=p)
                adopted+=1
                if adopted>=DISCOVER_LIMIT: break
        except Exception as e:
            log.debug("Discovery error: %s", e)
        for _ in range(DISCOVER_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ---------- Alerts & Guard ----------
def _cooldown_ok(key):
    last=_alert_last_sent.get(key,0.0)
    if time.time()-last>=COOLDOWN_SEC:
        _alert_last_sent[key]=time.time(); return True
    return False

def get_wallet_balances_snapshot():
    balances={}
    for k,v in list(_token_balances.items()):
        amt=float(v)
        if amt<=EPSILON: continue
        if k=="CRO": sym="CRO"
        elif isinstance(k,str) and k.startswith("0x"):
            meta=_token_meta.get(k,{})
            sym=(meta.get("symbol") or k[:8]).upper()
        else:
            sym=str(k).upper()
        balances[sym]=balances.get(sym,0.0)+amt
    return balances

def alerts_monitor_loop():
    send_telegram(f"🛰 Alerts monitor every {ALERTS_INTERVAL_MIN}m. Wallet 24h dump/pump: {DUMP_ALERT_24H_PCT}/{PUMP_ALERT_24H_PCT}.")
    while not shutdown_event.is_set():
        try:
            wallet_bal=get_wallet_balances_snapshot()
            for sym,amt in list(wallet_bal.items()):
                if amt<=EPSILON: continue
                price,ch24,ch2h,url=get_change_and_price_for_symbol_or_addr(sym)
                if not price or price<=0: continue
                if ch24 is not None:
                    if ch24>=PUMP_ALERT_24H_PCT and _cooldown_ok(f"24h_pump:{sym}"):
                        send_telegram(f"🚀 Pump Alert {sym} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{url}")
                    if ch24<=DUMP_ALERT_24H_PCT and _cooldown_ok(f"24h_dump:{sym}"):
                        send_telegram(f"⚠️ Dump Alert {sym} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{url}")
            data=read_json(data_file_for_today(), default={"entries":[]})
            seen=set()
            for e in data.get("entries",[]):
                if float(e.get("amount") or 0)>0:
                    sym=(e.get("token") or "?").upper()
                    addr=(e.get("token_addr") or "").lower()
                    key=addr if (addr and addr.startswith("0x")) else sym
                    if key in seen: continue
                    seen.add(key)
                    query=addr if (addr and addr.startswith("0x")) else sym
                    price,ch24,ch2h,url=get_change_and_price_for_symbol_or_addr(query)
                    if not price or price<=0: continue
                    ch = ch2h if (ch2h is not None) else ch24
                    if ch is None: continue
                    if ch>=PUMP_ALERT_24H_PCT and _cooldown_ok(f"risky:pump:{key}"):
                        send_telegram(f"🚀 Pump (recent) {sym} {ch:.2f}%\nPrice ${_format_price(price)}\n{url}")
                    if ch<=DUMP_ALERT_24H_PCT and _cooldown_ok(f"risky:dump:{key}"):
                        send_telegram(f"⚠️ Dump (recent) {sym} {ch:.2f}%\nPrice ${_format_price(price)}\n{url}")
        except Exception as e:
            log.exception("alerts monitor error: %s", e)
        for _ in range(ALERTS_INTERVAL_MIN*60):
            if shutdown_event.is_set(): break
            time.sleep(1)

def guard_monitor_loop():
    send_telegram(f"🛡 Guard monitor: {GUARD_WINDOW_MIN}m window, +{GUARD_PUMP_PCT}% / {GUARD_DROP_PCT}% / trailing {GUARD_TRAIL_DROP_PCT}%.")
    while not shutdown_event.is_set():
        try:
            dead=[]
            for key,st in list(_guard.items()):
                if time.time()-st["start_ts"]>GUARD_WINDOW_MIN*60:
                    dead.append(key); continue
                if key=="CRO": price=get_price_usd("CRO") or 0.0
                elif isinstance(key,str) and key.startswith("0x"): price=get_price_usd(key) or 0.0
                else:
                    meta=_token_meta.get(key,{}); sym=meta.get("symbol") or key
                    price=get_price_usd(sym) or 0.0
                if not price or price<=0: continue
                entry,peak=st["entry"],st["peak"]
                if price>peak: st["peak"]=price; peak=price
                pct_from_entry=(price-entry)/entry*100.0 if entry>0 else 0.0
                trail_from_peak=(price-peak)/peak*100.0 if peak>0 else 0.0
                sym=_token_meta.get(key,{}).get("symbol") or ("CRO" if key=="CRO" else (key[:6] if isinstance(key,str) else "ASSET"))
                if pct_from_entry>=GUARD_PUMP_PCT and _cooldown_ok(f"guard:pump:{key}"):
                    send_telegram(f"🟢 GUARD Pump {sym} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if pct_from_entry<=GUARD_DROP_PCT and _cooldown_ok(f"guard:drop:{key}"):
                    send_telegram(f"🔻 GUARD Drop {sym} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if trail_from_peak<=GUARD_TRAIL_DROP_PCT and _cooldown_ok(f"guard:trail:{key}"):
                    send_telegram(f"🟠 GUARD Trail {sym} {trail_from_peak:.2f}% from peak ${_format_price(peak)} → ${_format_price(price)}")
            for k in dead: _guard.pop(k,None)
        except Exception as e:
            log.exception("guard monitor error: %s", e)
        for _ in range(15):
            if shutdown_event.is_set(): break
            time.sleep(2)

# ---------- Today per-asset summary (/dailysum) ----------
def summarize_today_per_asset():
    path=data_file_for_today()
    data=read_json(path, default={"date": ymd(), "entries":[]})
    entries=data.get("entries",[])
    agg={}
    for e in entries:
        sym=(e.get("token") or "?").upper()
        addr=(e.get("token_addr") or "").lower()
        key=addr if addr.startswith("0x") else sym
        rec=agg.get(key)
        if not rec:
            rec={"symbol":sym,"token_addr":addr if addr else None,"buy_qty":0.0,"sell_qty":0.0,
                 "net_qty_today":0.0,"net_flow_today":0.0,"realized_today":0.0,"txs":[],
                 "last_price_seen":0.0}
            agg[key]=rec
        amt=float(e.get("amount") or 0.0)
        usd=float(e.get("usd_value") or 0.0)
        prc=float(e.get("price_usd") or 0.0)
        rp=float(e.get("realized_pnl") or 0.0)
        tm=(e.get("time","")[-8:]) or ""
        direction="IN" if amt>0 else "OUT"
        rec["txs"].append({"time":tm,"dir":direction,"amount":amt,"price":prc,"usd":usd,"realized":rp})
        if amt>0: rec["buy_qty"]+=amt
        if amt<0: rec["sell_qty"]+=-amt
        rec["net_qty_today"]+=amt
        rec["net_flow_today"]+=usd
        rec["realized_today"]+=rp
        if prc>0: rec["last_price_seen"]=prc

    result=[]
    _,_,_,_ = compute_holdings_merged()
    for key,rec in agg.items():
        if rec["token_addr"]:
            price_now=get_price_usd(rec["token_addr"]) or rec["last_price_seen"]; gkey=rec["token_addr"]
        else:
            price_now=get_price_usd(rec["symbol"]) or rec["last_price_seen"]; gkey=rec["symbol"]
        open_qty_now=_position_qty.get(gkey,0.0)
        open_cost_now=_position_cost.get(gkey,0.0)
        unreal_now=0.0
        if open_qty_now>EPSILON and _nonzero(price_now):
            unreal_now=open_qty_now*price_now - open_cost_now
        rec["price_now"]=price_now or 0.0
        rec["unreal_now"]=unreal_now
        result.append(rec)
    result.sort(key=lambda r: abs(r["net_flow_today"]), reverse=True)
    return result

def _format_daily_sum_message():
    per=summarize_today_per_asset()
    if not per: return f"🧾 Δεν υπάρχουν σημερινές κινήσεις ({ymd()})."
    tot_real=sum(float(r.get("realized_today",0.0)) for r in per)
    tot_flow=sum(float(r.get("net_flow_today",0.0)) for r in per)
    tot_unrl=sum(float(r.get("unreal_now",0.0) or 0.0) for r in per)
    per_sorted=sorted(per, key=lambda r: (abs(float(r.get("realized_today",0.0))), abs(float(r.get("net_flow_today",0.0)))), reverse=True)
    lines=[f"*🧾 Daily PnL (Today {ymd()}):*"]
    for r in per_sorted:
        tok=r.get("symbol") or "?"
        flow=float(r.get("net_flow_today",0.0))
        real=float(r.get("realized_today",0.0))
        qty =float(r.get("net_qty_today",0.0))
        pr  =float(r.get("price_now",0.0) or 0.0)
        un  =float(r.get("unreal_now",0.0) or 0.0)
        base=f"• {tok}: realized ${_format_amount(real)} | flow ${_format_amount(flow)} | qty {_format_amount(qty)}"
        if _nonzero(pr): base+=f" | price ${_format_price(pr)}"
        if _nonzero(un): base+=f" | unreal ${_format_amount(un)}"
        lines.append(base)
    lines.append("")
    lines.append(f"*Σύνολο realized σήμερα:* ${_format_amount(tot_real)}")
    lines.append(f"*Σύνολο net flow σήμερα:* ${_format_amount(tot_flow)}")
    if _nonzero(tot_unrl): lines.append(f"*Σύνολο unreal (open τώρα):* ${_format_amount(tot_unrl)}")
    return "\n".join(lines)

# ---------- Totals (today|month|all) ----------
def _iter_ledger_files_for_scope(scope:str):
    files=[]
    if scope=="today":
        files=[f"transactions_{ymd()}.json"]
    elif scope=="month":
        pref=month_prefix()
        try:
            for fn in os.listdir(DATA_DIR):
                if fn.startswith(f"transactions_{pref}") and fn.endswith(".json"):
                    files.append(fn)
        except:
            pass
    else:
        try:
            for fn in os.listdir(DATA_DIR):
                if fn.startswith("transactions_") and fn.endswith(".json"):
                    files.append(fn)
        except:
            pass
    files.sort()
    return [os.path.join(DATA_DIR,fn) for fn in files]

def _load_entries_for_totals(scope:str):
    entries=[]
    for path in _iter_ledger_files_for_scope(scope):
        data=read_json(path, default=None)
        if not isinstance(data,dict):
            continue
        for e in data.get("entries",[]):
            sym=(e.get("token") or "?").upper()
            amt=float(e.get("amount") or 0.0)
            usd=float(e.get("usd_value") or 0.0)
            realized=float(e.get("realized_pnl") or 0.0)
            side="IN" if amt>0 else "OUT"
            entries.append({
                "asset":sym,
                "side":side,
                "qty":abs(amt),
                "usd":usd,
                "realized_usd":realized,
            })
    return entries

def format_totals(scope:str):
    scope=(scope or "all").lower()
    rows=aggregate_per_asset(_load_entries_for_totals(scope))
    if not rows:
        return f"📊 Totals per Asset — {scope.capitalize()}: (no data)"
    lines=[f"📊 Totals per Asset — {scope.capitalize()}:"]
    for i,r in enumerate(rows,1):
        lines.append(
            f"{i}. {r['asset']}  "
            f"IN: {_format_amount(r['in_qty'])} (${_format_amount(r['in_usd'])}) | "
            f"OUT: {_format_amount(r['out_qty'])} (${_format_amount(r['out_usd'])}) | "
            f"REAL: ${_format_amount(r['realized_usd'])}"
        )
    totals_line = f"\nΣύνολο realized: ${_format_amount(sum(float(x['realized_usd']) for x in rows))}"
    return "\n".join([*lines, totals_line])

# ---------- Price + change helper (used by alerts & commands) ----------
def get_change_and_price_for_symbol_or_addr(sym_or_addr: str):
    pairs=[]
    try:
        key=(sym_or_addr or "").strip()
        if key.lower().startswith("0x") and len(key)==42:
            pairs=_pairs_for_token_addr(key)
        else:
            data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": key}, timeout=12)) or {}
            pairs=data.get("pairs") or []
        best=None; best_liq=-1.0
        for p in pairs:
            try:
                if str(p.get("chainId","")).lower()!="cronos":
                    continue
                liq=float((p.get("liquidity") or {}).get("usd") or 0)
                price=float(p.get("priceUsd") or 0)
                if price<=0: continue
                if liq>best_liq:
                    best_liq=liq; best=p
            except:
                continue
        if not best:
            return (None,None,None,None)
        price=float(best.get("priceUsd") or 0)
        ch24=None; ch2h=None
        try:
            ch=best.get("priceChange") or {}
            if "h24" in ch: ch24=float(ch.get("h24"))
            if "h2" in ch:  ch2h=float(ch.get("h2"))
        except:
            pass
        ds_url=f"https://dexscreener.com/cronos/{best.get('pairAddress')}"
        return (price, ch24, ch2h, ds_url)
    except:
        return (None,None,None,None)

# ---------- Wallet monitor loop ----------
def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    last_native_hashes=set()
    last_token_hashes=set()
    while not shutdown_event.is_set():
        try:
            for tx in fetch_latest_wallet_txs(limit=25):
                h=tx.get("hash")
                if h and h not in last_native_hashes:
                    last_native_hashes.add(h)
                    handle_native_tx(tx)
            for t in fetch_latest_token_txs(limit=100):
                h=t.get("hash")
                if h and h not in last_token_hashes:
                    last_token_hashes.add(h)
                    handle_erc20_tx(t)
            _replay_today_cost_basis()
        except Exception as e:
            log.exception("wallet monitor error: %s", e)
        for _ in range(WALLET_POLL):
            if shutdown_event.is_set():
                break
            time.sleep(1)

# ---------- Telegram long-poll ----------
import requests

def _tg_api(method: str, **params):
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r=requests.get(url, params=params, timeout=50)
        if r.status_code==200:
            return r.json()
    except Exception as e:
        log.debug("tg api error %s: %s", method, e)
    return None

def telegram_long_poll_loop():
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; telegram loop disabled.")
        return
    offset=None
    send_telegram("🤖 Telegram command handler online.")
    while not shutdown_event.is_set():
        try:
            resp=_tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
            if not resp or not resp.get("ok"):
                time.sleep(2); continue
            for upd in resp.get("result",[]):
                offset = upd["update_id"] + 1
                msg=upd.get("message") or {}
                chat_id=str(((msg.get("chat") or {}).get("id") or ""))
                if TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID)!=chat_id:
                    continue
                text=(msg.get("text") or "").strip()
                if not text:
                    continue
                _handle_command(text)
        except Exception as e:
            log.debug("telegram poll error: %s", e)
            time.sleep(2)

# ---------- Commands ----------
def _fmt_holdings_text():
    total, breakdown, unrealized, receipts = compute_holdings_merged()
    if not breakdown:
        return "📦 Κενά holdings."
    lines=["*📦 Holdings (merged):*"]
    for b in breakdown:
        lines.append(
            f"• {b['token']}: {_format_amount(b['amount'])}  @ ${_format_price(b.get('price_usd',0))}  = ${_format_amount(b.get('usd_value',0))}"
        )
    if receipts:
        lines.append("\n*Receipts:*")
        for r in receipts:
            lines.append(f"• {r['token']}: {_format_amount(r['amount'])}")
    lines.append(f"\nΣύνολο: ${_format_amount(total)}")
    if _nonzero(unrealized):
        lines.append(f"Unrealized: ${_format_amount(unrealized)}")
    return "\n".join(lines)

def _help_text():
    return ("❓ Commands: /status /diag /rescan /holdings /show /dailysum /showdaily /report "
            "/totals [today|month|all] /totalstoday /totalsmonth /pnl [today|month|all] "
            "/watch add|rm|list  /watchlist")

def _handle_command(text: str):
    t=text.strip()
    low=t.lower()

    if low == "/help":
        send_telegram(_help_text())
        return

    if low.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
        return

    if low.startswith("/diag"):
        send_telegram(
            "🔧 Diagnostics\n"
            f"WALLETADDRESS: {WALLET_ADDRESS}\n"
            f"CRONOSRPCURL set: {bool(CRONOS_RPC_URL)}\n"
            f"Etherscan key: {bool(ETHERSCAN_API)}\n"
            f"LOGSCANBLOCKS={LOG_SCAN_BLOCKS} LOGSCANCHUNK={LOG_SCAN_CHUNK}\n"
            f"TZ={TZ} INTRADAYHOURS={INTRADAY_HOURS} EOD={EOD_HOUR:02d}:{EOD_MINUTE:02d}\n"
            f"Alerts every: {ALERTS_INTERVAL_MIN}m | Pump/Dump: {PUMP_ALERT_24H_PCT}/{DUMP_ALERT_24H_PCT}\n"
            f"Tracked pairs: {', '.join(sorted(_tracked_pairs)) or '(none)'}"
        )
        return

    if low.startswith("/rescan"):
        cnt=rpc_discover_wallet_tokens()
        send_telegram(f"🔄 Rescan done. Positive tokens: {cnt}")
        return

    if low.startswith("/holdings") or low.startswith("/show_wallet_assets") or low.startswith("/showwalletassets") or low == "/show":
        send_telegram(_fmt_holdings_text()); return

    if low.startswith("/dailysum") or low.startswith("/showdaily"):
        send_telegram(_format_daily_sum_message()); return

    if low.startswith("/report"):
        send_telegram(build_day_report_text()); return

    if low.startswith("/totalstoday"):
        send_telegram(format_totals("today")); return

    if low.startswith("/totalsmonth"):
        send_telegram(format_totals("month")); return

    if low.startswith("/totals"):
        # allow "/totals", "/totals today", etc.
        parts=low.split()
        scope=parts[1] if len(parts)>1 else "all"
        if scope not in ("today","month","all"):
            scope="all"
        send_telegram(format_totals(scope)); return

    if low.startswith("/pnl"):
        parts=low.split()
        scope=parts[1] if len(parts)>1 else "all"
        if scope not in ("today","month","all"):
            scope="all"
        # Reuse totals formatting to show realized clearly
        send_telegram(format_totals(scope)); return

    # watchlist alias & UX
    if low.startswith("/watchlist"):
        send_telegram("👁 Tracked:\n" + ("\n".join(sorted(_tracked_pairs)) if _tracked_pairs else "None."))
        return

    if low == "/watch":
        send_telegram("Usage: /watch add <cronos/pair> | /watch rm <cronos/pair> | /watch list")
        return

    if low.startswith("/watch "):
        try:
            _, rest = low.split(" ",1)
            if rest.startswith("add "):
                pair=rest.split(" ",1)[1].strip().lower()
                if pair.startswith("cronos/"):
                    ensure_tracking_pair("cronos", pair.split("/",1)[1])
                    send_telegram(f"👁 Added {pair}")
                else:
                    send_telegram("Use format cronos/<pairAddress>")
            elif rest.startswith("rm "):
                pair=rest.split(" ",1)[1].strip().lower()
                if pair in _tracked_pairs:
                    _tracked_pairs.remove(pair)
                    send_telegram(f"🗑 Removed {pair}")
                else:
                    send_telegram("Pair not tracked.")
            elif rest.strip()=="list":
                send_telegram("👁 Tracked:\n"+("\n".join(sorted(_tracked_pairs)) if _tracked_pairs else "None."))
            else:
                send_telegram("Usage: /watch add <cronos/pair> | /watch rm <cronos/pair> | /watch list")
        except Exception as e:
            send_telegram("Usage: /watch add <cronos/pair> | /watch rm <cronos/pair> | /watch list")
        return

    # default -> help
    send_telegram(_help_text())

# ---------- Scheduler (intraday / EOD) ----------
def scheduler_loop():
    send_telegram("⏱ Scheduler online (intraday/EOD).")
    last_eod_date=None
    global _last_intraday_sent
    while not shutdown_event.is_set():
        try:
            now=now_dt()
            # Intraday ping/report every INTRADAY_HOURS
            if (_last_intraday_sent==0.0) or (time.time()-_last_intraday_sent >= INTRADAY_HOURS*3600):
                # lightweight status ping
                send_telegram("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
                _last_intraday_sent=time.time()
            # EOD report
            if now.hour==EOD_HOUR and now.minute>=EOD_MINUTE:
                if last_eod_date != ymd(now):
                    send_telegram(build_day_report_text())
                    last_eod_date = ymd(now)
        except Exception as e:
            log.debug("scheduler error: %s", e)
        for _ in range(30):
            if shutdown_event.is_set():
                break
            time.sleep(2)

# ---------- Main ----------
def main():
    load_ath()
    send_telegram("🟢 Starting Cronos DeFi Sentinel.")

    # Threads
    th_wallet=threading.Thread(target=wallet_monitor_loop, daemon=True)
    th_pairs =threading.Thread(target=monitor_tracked_pairs_loop, daemon=True)
    th_disc  =threading.Thread(target=discovery_loop, daemon=True)
    th_alerts=threading.Thread(target=alerts_monitor_loop, daemon=True)
    th_guard =threading.Thread(target=guard_monitor_loop, daemon=True)
    th_tg    =threading.Thread(target=telegram_long_poll_loop, daemon=True)
    th_sched =threading.Thread(target=scheduler_loop, daemon=True)

    for th in (th_wallet, th_pairs, th_disc, th_alerts, th_guard, th_tg, th_sched):
        th.start()

    def _graceful_shutdown(signum, frame):
        shutdown_event.set()
        send_telegram("🛑 Shutting down...")
    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    # Keep main alive
    while not shutdown_event.is_set():
        time.sleep(0.5)

if __name__=="__main__":
    main()
