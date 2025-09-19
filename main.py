#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
This canvas is reserved for the live view of your repository's canonical main.py
Repo: Zaikon13/wallet_monitor_Dex
Path: /main.py

👉 Paste here the current content from GitHub (Raw view) if it doesn't auto-populate.
Once it's in, I will apply the exact EOD scheduler patch on top (idempotent),
without changing anything else.

Notes:
- We keep CRO vs tCRO policy intact (no remapping here).
- We do NOT rewrite the file; only apply the queued patch if you ask me to.
- Comments with `# TODO:` are optional ideas, not active changes.
"""

# Paste the current main.py content from GitHub below this line.
import os, sys, time, json, threading, logging, signal
from collections import deque, defaultdict
from datetime import datetime, timedelta

from dotenv import load_dotenv
from zoneinfo import ZoneInfo
import schedule

# external helpers
from utils.http import safe_get, safe_json
from telegram.api import send_telegram
# Backward-compatible alias (startup/EOD patches expect send_telegram_message)
send_telegram_message = send_telegram
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

def get_price_usd(symbol_or_addr: str):
    if not symbol_or_addr: return None
    key = PRICE_ALIASES.get(symbol_or_addr.strip().lower(), symbol_or_addr.strip().lower())
    now = time.time()
    c = PRICE_CACHE.get(key)
    if c and (now - c[1] < PRICE_CACHE_TTL): return c[0]

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

    if (price is None) or (not price) or (float(price)<=0):
        hist=_history_price_fallback(symbol_or_addr, symbol_hint=symbol_or_addr)
        if hist and hist>0: price=float(hist)

    PRICE_CACHE[key]=(price, now)
    return price

def get_change_and_price_for_symbol_or_addr(sym_or_addr: str):
    if sym_or_addr.lower().startswith("0x") and len(sym_or_addr)==42:
        pairs=_pairs_for_token_addr(sym_or_addr)
    else:
        data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": sym_or_addr}, timeout=10)) or {}
        pairs=data.get("pairs") or []
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")).lower()!="cronos": continue
            liq=float((p.get("liquidity") or {}).get("usd") or 0)
            price=float(p.get("priceUsd") or 0)
            if price<=0: continue
            if liq>best_liq: best_liq, best = liq, p
        except: continue
    if not best: return (None,None,None,None)
    price=float(best.get("priceUsd") or 0)
    ch24=ch2h=None
    try:
        ch=best.get("priceChange") or {}
        if "h24" in ch: ch24=float(ch.get("h24"))
        if "h2"  in ch: ch2h=float(ch.get("h2"))
    except: pass
    ds_url=f"https://dexscreener.com/cronos/{best.get('pairAddress')}"
    return (price, ch24, ch2h, ds_url)

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
            if p>0:
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

    for b in breakdown:
        if b["token"].upper()=="TCRO": pass
        elif b["token"].upper()=="CRO" or b["token_addr"] is None:
            b["token"]="CRO"

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
        total+=rec["usd_value"]; breakdown.append(rec)

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

# ---------- EOD Daily Report (function + scheduler helper) ----------
# This section covers EOD daily report build & send.
# Nothing άλλο χρειάζεται να προστεθεί εκτός αν αλλάξουμε requirements.

def send_daily_report():
    try:
        text = build_day_report_text()
        # Telegram API already handles escaping/chunking inside telegram/api.py
        send_telegram_message(f"📒 Daily Report\n{text}")
    except Exception as e:
        logging.exception("Failed to build or send daily report: %s", e)
        send_telegram_message("⚠️ Failed to generate daily report.")

# Optional helper thread; call start_schedulers() from your main bootstrap (part2)
# to arm the EOD job and keep schedule.run_pending() ticking.
# If you already have a scheduler loop in part2, you can ignore this helper.

def start_schedulers():
    try:
        hh = max(0, min(23, int(EOD_HOUR)))
        mm = max(0, min(59, int(EOD_MINUTE)))
        eod_time = f"{hh:02d}:{mm:02d}"
        schedule.every().day.at(eod_time).do(send_daily_report)
        log.info("EOD scheduler armed at %s", eod_time)
    except Exception as ex:
        log.exception("Failed to arm EOD scheduler: %s", ex)
    # Run loop (non-blocking suggestion: spawn as a thread in part2)
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception:
            time.sleep(1)

# Continue in main_part2.py.....
# Continue in main_part2.py...
# ---------- Mini summaries & TX handlers ----------
# (unchanged code above)

# ---------- Main ----------
def _graceful_exit(signum, frame):
    try: send_telegram("🛑 Shutting down.")
    except: pass
    shutdown_event.set()

def main():
    load_ath()
    send_telegram("🟢 Starting Cronos DeFi Sentinel.")

    # seed discovery
    threading.Thread(target=discovery_loop, name="discovery", daemon=True).start()
    # monitors
    threading.Thread(target=wallet_monitor_loop, name="wallet", daemon=True).start()
    threading.Thread(target=monitor_tracked_pairs_loop, name="dex", daemon=True).start()
    threading.Thread(target=alerts_monitor_loop, name="alerts", daemon=True).start()
    threading.Thread(target=guard_monitor_loop, name="guard", daemon=True).start()
    threading.Thread(target=telegram_long_poll_loop, name="telegram", daemon=True).start()
    threading.Thread(target=_scheduler_loop, name="scheduler", daemon=True).start()
    # ---- NEW: EOD schedule helper ----
    threading.Thread(target=start_schedulers, name="eod_scheduler", daemon=True).start()

    # keep alive
    while not shutdown_event.is_set():
        time.sleep(1)

if __name__=="__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log.exception("fatal: %s", e)
        try: send_telegram(f"💥 Fatal error: {e}")
        except: pass
        sys.exit(1)
