#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Cronos DeFi Sentinel — main.py (updated)
- RPC snapshot (CRO + ERC-20)
- Dexscreener pricing (+history fallback)
- Cost-basis PnL (realized & unrealized)  [Decimal/FIFO in reports/ledger.py]
- Intraday/EOD reports
- Alerts & Guard window
- Telegram long-poll hardened (offset persistence & backoff)
- Merged holdings: prefer RPC quantities, use history only for price fallback; receipts (e.g., TCRO) never merged με CRO
"""

import os, sys, time, json, threading, logging, signal, math, random
from collections import deque, defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

# ----------------- external helpers (updated files πιο κάτω) -----------------
from utils.http import safe_get, safe_json
from telegram.api import send_telegram, escape_md
from reports.day_report import build_day_report_text as _compose_day_report
from reports.ledger import (
    append_ledger,
    update_cost_basis as ledger_update_cost_basis,
    replay_cost_basis_over_entries,
)
from reports.aggregates import aggregate_per_asset

# ----------------- Bootstrap / TZ -----------------
load_dotenv()
def _alias_env(src, dst):
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)
_alias_env("ALERTS_INTERVAL_MINUTES", "ALERTS_INTERVAL_MIN")
_alias_env("DISCOVER_REQUIRE_WCRO_QUOTE", "DISCOVER_REQUIRE_WCRO")

TZ = os.getenv("TZ", "Europe/Athens")
def _init_tz(tz_str: str | None):
    tz = tz_str or "Europe/Athens"
    os.environ["TZ"] = tz
    try:
        import time as _t
        if hasattr(_t, "tzset"): _t.tzset()
    except: pass
    return ZoneInfo(tz)
LOCAL_TZ = _init_tz(TZ)
def now_dt(): return datetime.now(LOCAL_TZ)
def ymd(dt=None): return (dt or now_dt()).strftime("%Y-%m-%d")
def month_prefix(dt=None): return (dt or now_dt()).strftime("%Y-%m")

# ----------------- ENV -----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or ""
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID") or ""
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API") or ""
CRONOS_RPC_URL     = os.getenv("CRONOS_RPC_URL") or ""

TOKENS = os.getenv("TOKENS","")
DEX_PAIRS = os.getenv("DEX_PAIRS","")

LOG_SCAN_BLOCKS = int(os.getenv("LOG_SCAN_BLOCKS","120000"))
LOG_SCAN_CHUNK  = int(os.getenv("LOG_SCAN_CHUNK","5000"))
WALLET_POLL     = int(os.getenv("WALLET_POLL","15"))
DEX_POLL        = int(os.getenv("DEX_POLL","60"))
PRICE_WINDOW    = int(os.getenv("PRICE_WINDOW","3"))
PRICE_MOVE_THRESHOLD = float(os.getenv("PRICE_MOVE_THRESHOLD","5"))
SPIKE_THRESHOLD      = float(os.getenv("SPIKE_THRESHOLD","8"))
MIN_VOLUME_FOR_ALERT = float(os.getenv("MIN_VOLUME_FOR_ALERT","0"))
INTRADAY_HOURS       = int(os.getenv("INTRADAY_HOURS","3"))
EOD_HOUR             = int(os.getenv("EOD_HOUR","23"))
EOD_MINUTE           = int(os.getenv("EOD_MINUTE","59"))

ALERTS_INTERVAL_MIN = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
DUMP_ALERT_24H_PCT  = float(os.getenv("DUMP_ALERT_24H_PCT","-15"))
PUMP_ALERT_24H_PCT  = float(os.getenv("PUMP_ALERT_24H_PCT","20"))

GUARD_WINDOW_MIN     = int(os.getenv("GUARD_WINDOW_MIN","60"))
GUARD_PUMP_PCT       = float(os.getenv("GUARD_PUMP_PCT","20"))
GUARD_DROP_PCT       = float(os.getenv("GUARD_DROP_PCT","-12"))
GUARD_TRAIL_DROP_PCT = float(os.getenv("GUARD_TRAIL_DROP_PCT","-8"))

RECEIPT_SYMBOLS = set([s.strip().upper() for s in os.getenv("RECEIPT_SYMBOLS","TCRO").split(",") if s.strip()])

# Canonical CRO pricing route
WCRO_USDT_PAIR = os.getenv("WCRO_USDT_PAIR", "").strip()  # e.g. "cronos/0xPAIRADDRESS"

# ----------------- Constants / Logging -----------------
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"
DATA_DIR         = "/app/data"
ATH_PATH         = os.path.join(DATA_DIR, "ath.json")
TG_OFFSET_PATH   = os.path.join(DATA_DIR, "telegram_offset.json")

os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("wallet-monitor")

# ----------------- Runtime & Locks -----------------
shutdown_event = threading.Event()

# shared state + locks
_token_balances = defaultdict(float)
_token_meta     = {}
_position_qty   = defaultdict(Decimal)   # now Decimal via ledger module, but we store numbers safely
_position_cost  = defaultdict(Decimal)
_guard          = {}                     # trailing stops per asset

_seen_tx_hashes      = set()
_seen_token_events   = set()
_seen_token_events_q = deque(maxlen=4000)
_seen_token_hashes   = set()
_seen_token_hashes_q = deque(maxlen=2000)

PRICE_CACHE, PRICE_CACHE_TTL = {}, 60
_HISTORY_LAST_PRICE = {}
ATH = {}
_alert_last_sent = {}
_last_prices, _price_history, _last_pair_tx = {}, {}, {}
_tracked_pairs, _known_pairs_meta = set(), {}

EPSILON = 1e-12
COOLDOWN_SEC = 60*30
_last_intraday_sent = 0.0

# Locks
BAL_LOCK    = threading.Lock()  # balances, meta, positions, seen sets
LEDGER_LOCK = threading.Lock()  # append_ledger & cost-basis calls
GUARD_LOCK  = threading.Lock()  # _guard dict
PAIR_LOCK   = threading.Lock()  # tracked pairs structs

# ----------------- Helpers -----------------
def _format_amount(a):
    try:
        a = float(a)
        if abs(a)>=1: return f"{a:,.4f}"
        if abs(a)>=0.0001: return f"{a:.6f}"
        return f"{a:.8f}"
    except: return str(a)

def _format_price(p):
    try:
        p = float(p)
        if p>=1: return f"{p:,.6f}"
        if p>=0.01: return f"{p:.6f}"
        if p>=1e-6: return f"{p:.8f}"
        return f"{p:.10f}"
    except: return str(p)

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

# ----------------- ATH -----------------
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
        send_telegram(f"🏆 New ATH {escape_md(key)}: ${_format_price(live_price)}")

# ----------------- Pricing (Dexscreener) -----------------
PRICE_ALIASES = {"tcro": "tcro"}  # DO NOT alias tCRO→CRO (receipt stays separate)

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
    for url in [f"{DEX_BASE_TOKENS}/cronos/{addr}", f"{DEX_BASE_TOKENS}/{addr}"]:
        data = safe_json(safe_get(url, timeout=10, retries=2)) or {}
        pairs = data.get("pairs") or []
        if pairs: return pairs
    data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": addr}, timeout=10, retries=2)) or {}
    return data.get("pairs") or []

def _history_price_fallback(query_key: str, symbol_hint: str=None):
    if not query_key: return None
    k=query_key.strip()
    if not k: return None
    if k.startswith("0x"):
        p=_HISTORY_LAST_PRICE.get(k)
        if p and p>0: return p
    sym=(symbol_hint or k).upper()
    p=_HISTORY_LAST_PRICE.get(sym)
    if p and p>0: return p
    if sym=="CRO":
        p=_HISTORY_LAST_PRICE.get("CRO")
        if p and p>0: return p
    return None

def _price_cro_from_canonical():
    # enforce WCRO/USDT pair if provided; else search wcro usdt then fallback
    if WCRO_USDT_PAIR:
        data = safe_json(safe_get(f"{DEX_BASE_PAIRS}/{WCRO_USDT_PAIR}", timeout=10, retries=2)) or {}
        pair = data.get("pair") or (data.get("pairs") or [None])[0]
        try:
            p = float((pair or {}).get("priceUsd") or 0)
            if p>0: return p
        except: pass
    # fallback search with preference
    for q in ["wcro usdt","wcro usdc","cro usdt","cro usdc"]:
        data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10, retries=2)) or {}
        price=_pick_best_price(data.get("pairs"))
        if price and price>0: return price
    return None

def get_price_usd(symbol_or_addr: str):
    if not symbol_or_addr: return None
    key = (PRICE_ALIASES.get(symbol_or_addr.strip().lower(), symbol_or_addr.strip().lower()))
    now = time.time()
    cached = PRICE_CACHE.get(key)
    if cached and (now - cached[1] < PRICE_CACHE_TTL):
        return cached[0]

    price=None
    try:
        if key in ("cro","wcro","w-cro","wrappedcro","wrapped cro"):
            price=_price_cro_from_canonical()
        elif key == "tcro":
            # receipt token: no market price
            price=None
        elif key.startswith("0x") and len(key)==42:
            price=_pick_best_price(_pairs_for_token_addr(key))
        else:
            for q in [key, f"{key} usdt", f"{key} wcro"]:
                data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10, retries=2)) or {}
                price=_pick_best_price(data.get("pairs"))
                if price: break
    except: price=None

    if (price is None) or (not price) or (float(price)<=0):
        hist=_history_price_fallback(key if key.startswith("0x") else symbol_or_addr, symbol_hint=symbol_or_addr)
        if hist and hist>0: price=float(hist)

    PRICE_CACHE[key]=(price, now)
    return price

def get_change_and_price_for_symbol_or_addr(sym_or_addr: str):
    if sym_or_addr.lower()=="tcro":
        return (None, None, None, None)
    if sym_or_addr.lower().startswith("0x") and len(sym_or_addr)==42:
        pairs=_pairs_for_token_addr(sym_or_addr)
    else:
        data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": sym_or_addr}, timeout=10, retries=2)) or {}
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
    ch = best.get("priceChange") or {}
    ch24 = float(ch.get("h24")) if "h24" in ch else None
    ch2h = float(ch.get("h2")) if "h2" in ch else None
    ds_url=f"https://dexscreener.com/cronos/{best.get('pairAddress')}"
    return (price, ch24, ch2h, ds_url)

# ----------------- Etherscan -----------------
def fetch_latest_wallet_txs(limit=25):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"txlist",
            "address":WALLET_ADDRESS,"startblock":0,"endblock":99999999,
            "page":1,"offset":limit,"sort":"desc","apikey":ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=2)) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

def fetch_latest_token_txs(limit=50):
    if not WALLET_ADDRESS or not ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"tokentx",
            "address":WALLET_ADDRESS,"startblock":0,"endblock":99999999,
            "page":1,"offset":limit,"sort":"desc","apikey":ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=2)) or {}
    if str(data.get("status","")).strip()=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

# ----------------- Cost-basis replay (today) -----------------
def _replay_today_cost_basis():
    path=data_file_for_today()
    data=read_json(path, default={"date": ymd(),"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    with LEDGER_LOCK:
        _position_qty.clear(); _position_cost.clear()
        total_realized=replay_cost_basis_over_entries(_position_qty,_position_cost,data.get("entries",[]))
        data["realized_pnl"]=float(total_realized)
        write_json(path,data)

# ----------------- History maps -----------------
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

# ----------------- RPC (minimal) -----------------
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
        if not WEB3.is_connected(): log.warning("Web3 not connected.")
        return WEB3.is_connected()
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
                with BAL_LOCK:
                    _token_balances[addr]=bal
                    _token_meta[addr]={"symbol":sym or addr[:8].upper(),"decimals":dec or 18}
                found_positive+=1
        except Exception as e:
            log.debug("discover balance/meta error %s: %s", addr, e)
            continue
    log.info("rpc_discover_wallet_tokens: positive-balance tokens discovered: %s", found_positive)
    return found_positive

# ----------------- Holdings (RPC / History / Merge) -----------------
def gather_all_known_token_contracts():
    known=set()
    with BAL_LOCK:
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
        with LEDGER_LOCK:
            rem_qty=float(_position_qty.get("CRO", Decimal(0)))
            rem_cost=float(_position_cost.get("CRO", Decimal(0)))
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
            with LEDGER_LOCK:
                rem_qty=float(_position_qty.get(addr, Decimal(0)))
                rem_cost=float(_position_cost.get(addr, Decimal(0)))
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
            key = addr_raw if (addr_raw and addr_raw.startswith("0x")) else (symbol_to_contract.get(sym_raw) or symbol_to_contract.get(symU) or symU or sym_raw or "?")
            _update(pos_qty,pos_cost,key,amt,pr)
    for k,v in list(pos_qty.items()):
        if abs(v)<1e-10: pos_qty[k]=0.0
    return pos_qty, pos_cost

def compute_holdings_usd_from_history_positions():
    pos_qty,pos_cost=rebuild_open_positions_from_history()
    total, breakdown, unrealized = 0.0, [], 0.0
    def _sym_for_key(key):
        if isinstance(key,str) and key.startswith("0x"):
            with BAL_LOCK:
                return _token_meta.get(key,{}).get("symbol") or key[:8].upper()
        return str(key)
    def _price_for(key, sym_hint):
        if isinstance(key,str) and key.startswith("0x"):
            p=get_price_usd(key)
        else:
            p=get_price_usd(sym_hint)
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
    """
    Merge RPC + History WITHOUT double counting:
      - If an asset exists in RPC, use **RPC amount**.
      - Use history only for price fallback & for assets missing from RPC.
      - Receipt symbols (RECEIPT_SYMBOLS) always excluded from CRO and total MTM; shown separately.
    """
    total_r, br_r, unrl_r = compute_holdings_usd_via_rpc()
    total_h, br_h, unrl_h = compute_holdings_usd_from_history_positions()

    def _key(b):
        addr=b.get("token_addr"); sym=(b.get("token") or "").upper()
        return (addr.lower() if (isinstance(addr,str) and addr.startswith("0x")) else sym)

    merged = {}
    source_map = {}

    # tag history
    for b in br_h or []:
        k=_key(b); 
        if not k: continue
        merged.setdefault(k, {"token":b["token"], "token_addr":b.get("token_addr"), "amount_history":float(b.get("amount") or 0.0),
                              "amount_rpc":0.0, "price_usd":float(b.get("price_usd") or 0.0)})
        source_map.setdefault(k, set()).add("H")

    # tag rpc
    for b in br_r or []:
        k=_key(b); 
        if not k: continue
        cur=merged.get(k) or {"token":b["token"], "token_addr":b.get("token_addr"), "amount_history":0.0, "amount_rpc":0.0, "price_usd":0.0}
        cur["token"]=b["token"] or cur["token"]
        cur["token_addr"]=b.get("token_addr", cur.get("token_addr"))
        cur["amount_rpc"]=cur.get("amount_rpc",0.0) + float(b.get("amount") or 0.0)
        # prefer positive price
        pr=float(b.get("price_usd") or 0.0)
        if pr>0: cur["price_usd"]=pr
        merged[k]=cur
        source_map.setdefault(k, set()).add("R")

    # build breakdown preferring RPC amount
    receipts, breakdown, total = [], [], 0.0
    for k, rec in merged.items():
        symU=(rec["token"] or "").upper()
        if symU in RECEIPT_SYMBOLS:  # receipt: list separately
            receipts.append({
                "token": rec["token"],
                "token_addr": rec.get("token_addr"),
                "amount": rec.get("amount_rpc") or rec.get("amount_history") or 0.0,
                "price_usd": None, "usd_value": 0.0
            })
            continue

        amt_rpc = float(rec.get("amount_rpc") or 0.0)
        amt_hist= float(rec.get("amount_history") or 0.0)
        use_amount = amt_rpc if amt_rpc>EPSILON else amt_hist

        # warn if divergence big
        if amt_rpc>EPSILON and abs(amt_hist-amt_rpc) > max(0.000001, 0.01*amt_rpc):
            log.warning("merge divergence %s: RPC=%s vs HIST=%s", k, amt_rpc, amt_hist)

        pr = float(rec.get("price_usd") or 0.0)
        if pr<=0:
            # fallback to history price if needed
            pr = float(_history_price_fallback(k if str(k).startswith("0x") else rec["token"], symbol_hint=rec["token"]) or 0.0)
        usd = use_amount * pr
        item={"token": rec["token"], "token_addr": rec.get("token_addr"),
              "amount": use_amount, "price_usd": pr, "usd_value": usd}
        total += usd
        breakdown.append(item)

    breakdown.sort(key=lambda b: float(b.get("usd_value",0.0)), reverse=True)
    # unrealized: recompute vs positions with current prices
    unrealized=0.0
    with LEDGER_LOCK:
        for it in breakdown:
            k = it["token_addr"] if (it["token_addr"] and str(it["token_addr"]).startswith("0x")) else (it["token"].upper())
            qty = float(_position_qty.get(k, Decimal(0)))
            cost= float(_position_cost.get(k, Decimal(0)))
            p   = float(it.get("price_usd") or 0.0)
            if qty>EPSILON and _nonzero(p):
                unrealized += (qty*p - cost)
    return total, breakdown, unrealized, receipts

# ----------------- Day report wrapper -----------------
def build_day_report_text():
    date_str=ymd()
    path=data_file_for_today()
    data=read_json(path, default={"date":date_str,"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    entries=data.get("entries",[])
    net_flow=float(data.get("net_usd_flow",0.0))
    realized_today_total=float(data.get("realized_pnl",0.0))
    holdings_total, breakdown, unrealized, _receipts = compute_holdings_merged()
    return _compose_day_report(
        date_str=date_str, entries=entries, net_flow=net_flow,
        realized_today_total=realized_today_total, holdings_total=holdings_total,
        breakdown=breakdown, unrealized=unrealized, data_dir=DATA_DIR, local_tz=LOCAL_TZ
    )

# ----------------- TX Handlers (with locks) -----------------
def _remember_token_event(key_tuple):
    with BAL_LOCK:
        if key_tuple in _seen_token_events: return False
        _seen_token_events.add(key_tuple); _seen_token_events_q.append(key_tuple)
        if len(_seen_token_events_q)==_seen_token_events_q.maxlen:
            while len(_seen_token_events)>_seen_token_events_q.maxlen:
                old=_seen_token_events_q.popleft()
                if old in _seen_token_events: _seen_token_events.remove(old)
        return True

def _remember_token_hash(h):
    if not h: return True
    with BAL_LOCK:
        if h in _seen_token_hashes: return False
        _seen_token_hashes.add(h); _seen_token_hashes_q.append(h)
        if len(_seen_token_hashes_q)==_seen_token_hashes_q.maxlen:
            while len(_seen_token_hashes)>_seen_token_hashes_q.maxlen:
                old=_seen_token_hashes_q.popleft()
                if old in _seen_token_hashes: _seen_token_hashes.remove(old)
        return True

def _mini_summary_line(token_key, symbol_shown):
    with LEDGER_LOCK:
        open_qty=float(_position_qty.get(token_key, Decimal(0.0)))
        open_cost=float(_position_cost.get(token_key, Decimal(0.0)))
    if token_key=="CRO": live=get_price_usd("CRO") or 0.0
    elif isinstance(token_key,str) and token_key.startswith("0x"): live=get_price_usd(token_key) or 0.0
    else: live=get_price_usd(symbol_shown) or 0.0
    unreal=0.0
    if open_qty>EPSILON and _nonzero(live): unreal=open_qty*live - open_cost
    send_telegram(
        f"• {'Open' if open_qty>0 else 'Flat'} {escape_md(symbol_shown)} {_format_amount(open_qty)} @ live ${_format_price(live)}\n"
        f"   Avg: ${_format_price((open_cost/open_qty) if open_qty>EPSILON else 0)} | Unreal: ${_format_amount(unreal)}"
    )

def handle_native_tx(tx: dict):
    h=tx.get("hash")
    with BAL_LOCK:
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
    with BAL_LOCK:
        _token_balances["CRO"]+=sign*amount_cro
        _token_meta["CRO"]={"symbol":"CRO","decimals":18}
    with LEDGER_LOCK:
        realized=ledger_update_cost_basis(_position_qty,_position_cost,"CRO",Decimal(sign*amount_cro),Decimal(price or 0.0))
        append_ledger({
            "time": dt.strftime("%Y-%m-%d %H:%M:%S"), "txhash": h, "type":"native",
            "token":"CRO", "token_addr": None, "amount": sign*amount_cro,
            "price_usd": price, "usd_value": usd_value, "realized_pnl": float(realized),
            "from": frm, "to": to
        })
    link=CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\n"
        f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount_cro:.6f} CRO\nPrice: ${_format_price(price)}\nUSD value: ${_format_amount(usd_value)}"
    )

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
    price = get_price_usd(token_addr if (token_addr and token_addr.startswith("0x")) else symbol) or 0.0
    usd_value=sign*amount*(price or 0.0)
    key=token_addr if token_addr else symbol
    with BAL_LOCK:
        _token_balances[key]+=sign*amount
        if abs(_token_balances[key])<1e-10: _token_balances[key]=0.0
        _token_meta[key]={"symbol":symbol,"decimals":decimals}
    if _nonzero(price):
        try: update_ath(token_addr if token_addr else symbol, price)
        except: pass
    with LEDGER_LOCK:
        realized=ledger_update_cost_basis(_position_qty,_position_cost,key,Decimal(sign*amount),Decimal(price or 0.0))
        append_ledger({
            "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "txhash": h or None, "type":"erc20", "token": symbol, "token_addr": token_addr or None,
            "amount": sign*amount, "price_usd": price or 0.0, "usd_value": usd_value,
            "realized_pnl": float(realized), "from": frm, "to": to
        })
    send_telegram(
        f"Token TX ({'IN' if sign>0 else 'OUT'}) {escape_md(symbol)}\n"
        f"Hash: {CRONOS_TX.format(txhash=h)}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {escape_md(symbol)}\nPrice: ${_format_price(price)}\nUSD value: ${_format_amount(usd_value)}"
    )
    send_telegram(f"• {'BUY' if sign>0 else 'SELL'} {escape_md(symbol)} {_format_amount(abs(amount))} @ live ${_format_price(price)}")
    _mini_summary_line(key, symbol)
    if sign>0 and _nonzero(price):
        with GUARD_LOCK:
            _guard[key]={"entry":float(price),"peak":float(price),"start_ts":time.time()}

# ----------------- Dex monitor & discovery (unchanged logic, safer calls) -----------------
def slug(chain: str, pair_address: str) -> str: return f"{chain}/{pair_address}".lower()
def fetch_pair(slg_str: str): return safe_json(safe_get(f"{DEX_BASE_PAIRS}/{slg_str}", timeout=12, retries=2))
def fetch_token_pairs(chain: str, token_address: str):
    data=safe_json(safe_get(f"{DEX_BASE_TOKENS}/{chain}/{token_address}", timeout=12, retries=2)) or {}
    return data.get("pairs") or []
def fetch_search(query: str):
    data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=15, retries=2)) or {}
    return data.get("pairs") or []
def ensure_tracking_pair(chain: str, pair_address: str, meta: dict=None):
    s=slug(chain, pair_address)
    with PAIR_LOCK:
        if s in _tracked_pairs: return
        _tracked_pairs.add(s); _last_prices[s]=None; _last_pair_tx[s]=None
        _price_history[s]=deque(maxlen=PRICE_WINDOW)
        if meta: _known_pairs_meta[s]=meta
    ds_link=f"https://dexscreener.com/{chain}/{pair_address}"
    sym=None
    if isinstance(meta,dict):
        bt=meta.get("baseToken") or {}; sym=bt.get("symbol")
    title=f"{sym} ({s})" if sym else s
    send_telegram(f"🆕 Now monitoring pair: {escape_md(title)}\n{ds_link}")
def update_price_history(slg, price):
    with PAIR_LOCK:
        hist=_price_history.get(slg) or deque(maxlen=PRICE_WINDOW)
        _price_history[slg]=hist; hist.append(price); _last_prices[slg]=price
def detect_spike(slg):
    with PAIR_LOCK:
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
    with PAIR_LOCK:
        empty = not _tracked_pairs
    if empty:
        log.info("No tracked pairs; monitor waits.")
    else:
        with PAIR_LOCK:
            send_telegram(f"🚀 Dex monitor started: {escape_md(', '.join(sorted(_tracked_pairs)))}")
    while not shutdown_event.is_set():
        with PAIR_LOCK:
            pairs = list(_tracked_pairs)
        if not pairs:
            time.sleep(DEX_POLL); continue
        for s in pairs:
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
                                send_telegram(f"🚨 Spike on {escape_md(symbol)}: {spike_pct:.2f}%\nPrice: ${_format_price(price_val)}")
                                with PAIR_LOCK:
                                    _price_history[s].clear(); _last_prices[s]=price_val
                with PAIR_LOCK:
                    prev=_last_prices.get(s)
                if prev and price_val and prev>0:
                    delta=(price_val-prev)/prev*100.0
                    if abs(delta)>=PRICE_MOVE_THRESHOLD and _pair_cooldown_ok(f"move:{s}"):
                        bt=pair.get("baseToken") or {}; symbol=bt.get("symbol") or s
                        send_telegram(f"📈 Price move on {escape_md(symbol)}: {delta:.2f}%\nPrice: ${_format_price(price_val)} (prev ${_format_price(prev)})")
                        with PAIR_LOCK:
                            _last_prices[s]=price_val
                last_tx=(pair.get("lastTx") or {}).get("hash")
                if last_tx:
                    with PAIR_LOCK:
                        prev_tx=_last_pair_tx.get(s)
                    if prev_tx!=last_tx and _pair_cooldown_ok(f"trade:{s}"):
                        with PAIR_LOCK:
                            _last_pair_tx[s]=last_tx
                        bt=pair.get("baseToken") or {}; symbol=bt.get("symbol") or s
                        send_telegram(f"🔔 New trade on {escape_md(symbol)}\nTx: {CRONOS_TX.format(txhash=last_tx)}")
            except Exception as e:
                log.debug("pairs loop error %s: %s", s, e)
        # poll cadence with slight jitter
        t = DEX_POLL + random.uniform(-2, 2)
        for _ in range(max(1,int(t))):
            if shutdown_event.is_set(): break
            time.sleep(1)

def _pair_passes_filters(p):
    try:
        if str(p.get("chainId","")).lower()!="cronos": return False
        bt=p.get("baseToken") or {}; qt=p.get("quoteToken") or {}
        base_sym=(bt.get("symbol") or "").upper(); quote_sym=(qt.get("symbol") or "").upper()
        if (os.getenv("DISCOVER_REQUIRE_WCRO","false").lower() in ("1","true","yes","on")) and quote_sym!="WCRO": return False
        wl=[s.strip().upper() for s in os.getenv("DISCOVER_BASE_WHITELIST","").split(",") if s.strip()]
        bl=[s.strip().upper() for s in os.getenv("DISCOVER_BASE_BLACKLIST","").split(",") if s.strip()]
        if wl and base_sym not in wl: return False
        if bl and base_sym in bl: return False
        liq=float((p.get("liquidity") or {}).get("usd") or 0)
        if liq < float(os.getenv("DISCOVER_MIN_LIQ_USD","30000")): return False
        vol24=float((p.get("volume") or {}).get("h24") or 0)
        if vol24 < float(os.getenv("DISCOVER_MIN_VOL24_USD","5000")): return False
        ch=p.get("priceChange") or {}; best_change=0.0
        for k in ("h1","h4","h6","h24"):
            if k in ch:
                try: best_change=max(best_change, abs(float(ch[k])))
                except: pass
        if best_change < float(os.getenv("DISCOVER_MIN_ABS_CHANGE_PCT","10")): return False
        created_ms=p.get("pairCreatedAt")
        if created_ms:
            age_h=(time.time()*1000 - float(created_ms))/1000/3600.0
            if age_h > float(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS","24")): return False
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
    if (os.getenv("DISCOVER_ENABLED","true").lower() not in ("1","true","yes","on")):
        log.info("Discovery disabled."); return
    send_telegram("🧭 Dexscreener auto-discovery enabled (Cronos).")
    last_ok = 0
    while not shutdown_event.is_set():
        try:
            found=fetch_search(os.getenv("DISCOVER_QUERY","cronos"))
            adopted=0
            for p in found or []:
                if not _pair_passes_filters(p): continue
                pair_addr=p.get("pairAddress")
                if not pair_addr: continue
                s=slug("cronos", pair_addr)
                with PAIR_LOCK:
                    if s in _tracked_pairs: continue
                ensure_tracking_pair("cronos", pair_addr, meta=p)
                adopted+=1
                if adopted>=int(os.getenv("DISCOVER_LIMIT","10")): break
            last_ok = time.time()
        except Exception as e:
            log.debug("Discovery error: %s", e)
        # exponential-ish backoff with cap
        base = int(os.getenv("DISCOVER_POLL","120"))
        delay = base if (time.time()-last_ok<600) else min(900, base*2)
        delay += random.randint(0,5)
        for _ in range(delay):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------- Alerts & Guard -----------------
def _cooldown_ok(key):
    last=_alert_last_sent.get(key,0.0)
    if time.time()-last>=COOLDOWN_SEC:
        _alert_last_sent[key]=time.time(); return True
    return False

def get_wallet_balances_snapshot():
    balances={}
    with BAL_LOCK:
        items=list(_token_balances.items())
        metas=dict(_token_meta)
    for k,v in items:
        amt=float(v)
        if amt<=EPSILON: continue
        if k=="CRO": sym="CRO"
        elif isinstance(k,str) and k.startswith("0x"):
            sym=(metas.get(k,{}).get("symbol") or k[:8]).upper()
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
                if sym.upper() in RECEIPT_SYMBOLS or amt<=EPSILON: continue
                price,ch24,ch2h,url=get_change_and_price_for_symbol_or_addr(sym)
                if not price or price<=0: continue
                if ch24 is not None:
                    if ch24>=PUMP_ALERT_24H_PCT and _cooldown_ok(f"24h_pump:{sym}"):
                        send_telegram(f"🚀 Pump Alert {escape_md(sym)} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{url}")
                    if ch24<=DUMP_ALERT_24H_PCT and _cooldown_ok(f"24h_dump:{sym}"):
                        send_telegram(f"⚠️ Dump Alert {escape_md(sym)} 24h {ch24:.2f}%\nPrice ${_format_price(price)}\n{url}")
            data=read_json(data_file_for_today(), default={"entries":[]})
            seen=set()
            for e in data.get("entries",[]):
                if float(e.get("amount") or 0)>0:
                    sym=(e.get("token") or "?").upper()
                    if sym in RECEIPT_SYMBOLS: continue
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
                        send_telegram(f"🚀 Pump (recent) {escape_md(sym)} {ch:.2f}%\nPrice ${_format_price(price)}\n{url}")
                    if ch<=DUMP_ALERT_24H_PCT and _cooldown_ok(f"risky:dump:{key}"):
                        send_telegram(f"⚠️ Dump (recent) {escape_md(sym)} {ch:.2f}%\nPrice ${_format_price(price)}\n{url}")
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
            with GUARD_LOCK:
                items=list(_guard.items())
            for key,st in items:
                if time.time()-st.get("start_ts",0)>GUARD_WINDOW_MIN*60:
                    dead.append(key); continue
                if key=="CRO": price=get_price_usd("CRO") or 0.0
                elif isinstance(key,str) and key.startswith("0x"): price=get_price_usd(key) or 0.0
                else:
                    with BAL_LOCK:
                        sym=( _token_meta.get(key,{}).get("symbol") or key )
                    price=get_price_usd(sym) or 0.0
                if not price or price<=0: continue
                entry=st.get("entry",0.0); peak=st.get("peak",entry)
                if entry<=0:
                    with GUARD_LOCK:
                        _guard[key]={"entry":float(price),"peak":float(price),"start_ts":time.time()}
                    entry=price; peak=price
                if price>peak:
                    with GUARD_LOCK:
                        st=_guard.get(key,st); st["peak"]=price; peak=price
                pct_from_entry=(price-entry)/entry*100.0 if entry>0 else 0.0
                trail_from_peak=(price-peak)/peak*100.0 if peak>0 else 0.0
                with BAL_LOCK:
                    sym=_token_meta.get(key,{}).get("symbol") or ("CRO" if key=="CRO" else (key[:6] if isinstance(key,str) else "ASSET"))
                if pct_from_entry>=GUARD_PUMP_PCT and _cooldown_ok(f"guard:pump:{key}"):
                    send_telegram(f"🟢 GUARD Pump {escape_md(sym)} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if pct_from_entry<=GUARD_DROP_PCT and _cooldown_ok(f"guard:drop:{key}"):
                    send_telegram(f"🔻 GUARD Drop {escape_md(sym)} {pct_from_entry:.2f}% (entry ${_format_price(entry)} → ${_format_price(price)})")
                if trail_from_peak<=GUARD_TRAIL_DROP_PCT and _cooldown_ok(f"guard:trail:{key}"):
                    send_telegram(f"🟠 GUARD Trail {escape_md(sym)} {trail_from_peak:.2f}% from peak ${_format_price(peak)} → ${_format_price(price)}")
            if dead:
                with GUARD_LOCK:
                    for k in dead: _guard.pop(k, None)
        except Exception as e:
            log.exception("guard monitor error: %s", e)
        for _ in range(15):
            if shutdown_event.is_set(): break
            time.sleep(2)

# ----------------- Daily sum & Totals -----------------
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
        with LEDGER_LOCK:
            open_qty_now=float(_position_qty.get(gkey, Decimal(0)))
            open_cost_now=float(_position_cost.get(gkey, Decimal(0)))
        unreal_now=0.0
        if open_qty_now>EPSILON and _nonzero(price_now):
            unreal_now=open_qty_now*price_now - open_cost_now
        rec["price_now"]=price_now or 0.0
        rec["unreal_now"]=unreal_now
        result.append(rec)
    result.sort(key=lambda r: (abs(r["net_flow_today"]), abs(r["realized_today"])), reverse=True)
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
        base=f"• {escape_md(tok)}: realized ${_format_amount(real)} | flow ${_format_amount(flow)} | qty {_format_amount(qty)}"
        if _nonzero(pr): base+=f" | price ${_format_price(pr)}"
        if _nonzero(un): base+=f" | unreal ${_format_amount(un)}"
        lines.append(base)
    lines.append("")
    lines.append(f"*Σύνολο realized σήμερα:* ${_format_amount(tot_real)}")
    lines.append(f"*Σύνολο net flow σήμερα:* ${_format_amount(tot_flow)}")
    if _nonzero(tot_unrl): lines.append(f"*Σύνολο unreal (open τώρα):* ${_format_amount(tot_unrl)}")
    return "\n".join(lines)

def _iter_ledger_files_for_scope(scope:str):
    files=[]
    if scope=="today":
        files=[f"transactions_{ymd()}.json"]
    elif scope=="month":
        pref=month_prefix()
        try:
            for fn in os.listdir(DATA_DIR):
                if fn.startswith(f"transactions_{pref}") and fn.endswith(".json"): files.append(fn)
        except: pass
    else:
        try:
            for fn in os.listdir(DATA_DIR):
                if fn.startswith("transactions_") and fn.endswith(".json"): files.append(fn)
        except: pass
    files.sort()
    return [os.path.join(DATA_DIR,fn) for fn in files]

def _load_entries_for_totals(scope:str):
    entries=[]
    for path in _iter_ledger_files_for_scope(scope):
        data=read_json(path, default=None)
        if not isinstance(data,dict): continue
        for e in data.get("entries",[]):
            sym=(e.get("token") or "?").upper()
            amt=float(e.get("amount") or 0.0)
            usd=float(e.get("usd_value") or 0.0)
            realized=float(e.get("realized_pnl") or 0.0)
            side="IN" if amt>0 else "OUT"
            entries.append({"asset":sym,"side":side,"qty":abs(amt),"usd":usd,"realized_usd":realized})
    return entries

def format_totals(scope:str):
    scope=(scope or "all").lower()
    rows=aggregate_per_asset(_load_entries_for_totals(scope))
    if not rows: return f"📊 Totals per Asset — {scope.capitalize()}: (no data)"
    lines=[f"📊 Totals per Asset — {scope.capitalize()}:"]
    for i,r in enumerate(rows,1):
        lines.append(
            f"{i}. {escape_md(r['asset'])}  "
            f"IN: {_format_amount(r['in_qty'])} (${_format_amount(r['in_usd'])}) | "
            f"OUT: {_format_amount(r['out_qty'])} (${_format_amount(r['out_usd'])}) | "
            f"NET: {_format_amount(r['net_qty'])} (${_format_amount(r['net_usd'])}) | "
            f"REAL: ${_format_amount(r['realized_usd'])} | TX: {int(r.get('tx_count',0))}"
        )
    totals_line = f"\nΣύνολο realized: ${_format_amount(sum(float(x['realized_usd']) for x in rows))}"
    lines.append(totals_line)
    return "\n".join(lines)

# ----------------- Wallet monitor loop -----------------
def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    last_native_hashes=set()
    last_token_hashes=set()
    while not shutdown_event.is_set():
        try:
            for tx in fetch_latest_wallet_txs(limit=25):
                h=tx.get("hash")
                if h and h not in last_native_hashes:
                    last_native_hashes.add(h); handle_native_tx(tx)
            for t in fetch_latest_token_txs(limit=100):
                h=t.get("hash")
                if h and h not in last_token_hashes:
                    last_token_hashes.add(h); handle_erc20_tx(t)
            _replay_today_cost_basis()
        except Exception as e:
            log.exception("wallet monitor error: %s", e)
        for _ in range(WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)

# ----------------- Telegram long-poll -----------------
import requests
def _tg_api(method: str, **params):
    url=f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r=requests.get(url, params=params, timeout=50)
        if r.status_code==200: return r.json()
    except Exception as e:
        log.debug("tg api error %s: %s", method, e)
    return None

def _load_offset():
    data=read_json(TG_OFFSET_PATH, default={"offset": None})
    return data.get("offset")
def _save_offset(off):
    write_json(TG_OFFSET_PATH, {"offset": off})

def telegram_long_poll_loop():
    if not TELEGRAM_BOT_TOKEN:
        log.warning("No TELEGRAM_BOT_TOKEN; telegram loop disabled."); return
    offset=_load_offset()
    send_telegram("🤖 Telegram command handler online.")
    backoff=2
    while not shutdown_event.is_set():
        try:
            resp=_tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
            if not resp or not resp.get("ok"):
                time.sleep(min(30, backoff)); backoff=min(30, backoff*2); continue
            backoff=2
            for upd in resp.get("result",[]):
                offset = upd["update_id"] + 1
                _save_offset(offset)
                msg=upd.get("message") or {}
                chat_id=str(((msg.get("chat") or {}).get("id") or ""))
                if TELEGRAM_CHAT_ID and str(TELEGRAM_CHAT_ID)!=chat_id:
                    continue
                text=(msg.get("text") or "").strip()
                if not text: continue
                _handle_command(text)
        except Exception as e:
            log.debug("telegram poll error: %s", e)
            time.sleep(min(30, backoff + random.uniform(0,2))); backoff=min(30, backoff*2)

def _fmt_holdings_text():
    total, breakdown, unrealized, receipts = compute_holdings_merged()
    if not breakdown and not receipts:
        return "📦 Κενά holdings."
    lines=["*📦 Holdings (merged):*"]
    for b in breakdown:
        lines.append(f"• {escape_md(b['token'])}: {_format_amount(b['amount'])}  @ ${_format_price(b.get('price_usd',0))}  = ${_format_amount(b.get('usd_value',0))}")
    if receipts:
        lines.append("\n*Receipts:*")
        for r in receipts:
            lines.append(f"• {escape_md(r['token'])}: {_format_amount(r['amount'])}")
    lines.append(f"\nΣύνολο: ${_format_amount(total)}")
    if _nonzero(unrealized):
        lines.append(f"Unrealized: ${_format_amount(unrealized)}")
    return "\n".join(lines)

def _handle_command(text: str):
    t=text.strip()
    low=t.lower()
    if low.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor, Dex monitor, Alerts & Guard active.")
    elif low.startswith("/diag") or low.startswith("/status"):
        with PAIR_LOCK:
            tracked = ', '.join(sorted(_tracked_pairs)) or '(none)'
        send_telegram(
            "🔧 Diagnostics\n"
            f"WALLETADDRESS: `{escape_md(WALLET_ADDRESS)}`\n"
            f"CRONOSRPCURL set: {bool(CRONOS_RPC_URL)}\n"
            f"Etherscan key: {bool(ETHERSCAN_API)}\n"
            f"LOGSCANBLOCKS={LOG_SCAN_BLOCKS} LOGSCANCHUNK={LOG_SCAN_CHUNK}\n"
            f"TZ={TZ} INTRADAYHOURS={INTRADAY_HOURS} EOD={EOD_HOUR:02d}:{EOD_MINUTE:02d}\n"
            f"Alerts every: {ALERTS_INTERVAL_MIN}m | Pump/Dump: {PUMP_ALERT_24H_PCT}/{DUMP_ALERT_24H_PCT}\n"
            f"Tracked pairs: {escape_md(tracked)}"
        )
    elif low.startswith("/rescan"):
        cnt=rpc_discover_wallet_tokens()
        send_telegram(f"🔄 Rescan done. Positive tokens: {cnt}")
    elif low.startswith("/holdings") or low.startswith("/show_wallet_assets") or low.startswith("/showwalletassets") or low=="/show":
        send_telegram(_fmt_holdings_text())
    elif low.startswith("/dailysum") or low.startswith("/showdaily"):
        send_telegram(_format_daily_sum_message())
    elif low.startswith("/report"):
        send_telegram(build_day_report_text())
    elif low.startswith("/totals"):
        parts=low.split()
        scope="all"
        if len(parts)>1 and parts[1] in ("today","month","all"):
            scope=parts[1]
        send_telegram(format_totals(scope))
    elif low.startswith("/totalstoday"):
        send_telegram(format_totals("today"))
    elif low.startswith("/totalsmonth"):
        send_telegram(format_totals("month"))
    elif low.startswith("/pnl"):
        parts=low.split()
        scope=parts[1] if len(parts)>1 and parts[1] in ("today","month","all") else "all"
        send_telegram(format_totals(scope))
    elif low.startswith("/watch "):
        try:
            _, rest = low.split(" ",1)
            if rest.startswith("add "):
                pair=rest.split(" ",1)[1].strip().lower()
                if pair.startswith("cronos/"):
                    ensure_tracking_pair("cronos", pair.split("/",1)[1])
                    send_telegram(f"👁 Added {escape_md(pair)}")
                else:
                    send_telegram("Use format cronos/<pairAddress>")
            elif rest.startswith("rm ""):
                pair=rest.split(" ",1)[1].strip().lower()
                with PAIR_LOCK:
                    if pair in _tracked_pairs:
                        _tracked_pairs.remove(pair)
                        send_telegram(f"🗑 Removed {escape_md(pair)}")
                    else: send_telegram("Pair not tracked.")
            elif rest.strip()=="list":
                with PAIR_LOCK:
                    lst = "👁 Tracked:\n"+"\n".join(sorted(_tracked_pairs)) if _tracked_pairs else "None."
                send_telegram(lst)
            else:
                send_telegram("Usage: /watch add <cronos/pair> | /watch rm <cronos/pair> | /watch list")
        except Exception as e:
            send_telegram(f"Watch error: {escape_md(str(e))}")
    else:
        send_telegram("❓ Commands: /status /diag /rescan /holdings /show /dailysum /report /totals [today|month|all] /totalstoday /totalsmonth /pnl [scope] /watch ...")

# ----------------- Scheduler & Main -----------------
def _scheduler_loop():
    global _last_intraday_sent
    send_telegram("⏱ Scheduler online (intraday/EOD).")
    while not shutdown_event.is_set():
        try:
            now=now_dt()
            if _last_intraday_sent<=0 or (time.time()-_last_intraday_sent)>=INTRADAY_HOURS*3600:
                send_telegram(_format_daily_sum_message()); _last_intraday_sent=time.time()
            if now.hour==EOD_HOUR and now.minute==EOD_MINUTE:
                send_telegram(build_day_report_text())
                time.sleep(65)
        except Exception as e:
            log.debug("scheduler error: %s", e)
        for _ in range(20):
            if shutdown_event.is_set(): break
            time.sleep(3)

def _graceful_exit(signum, frame):
    try: send_telegram("🛑 Shutting down.")
    except: pass
    shutdown_event.set()

def main():
    load_ath()
    send_telegram("🟢 Starting Cronos DeFi Sentinel.")
    threads = [
        threading.Thread(target=discovery_loop, name="discovery"),
        threading.Thread(target=wallet_monitor_loop, name="wallet"),
        threading.Thread(target=monitor_tracked_pairs_loop, name="dex"),
        threading.Thread(target=alerts_monitor_loop, name="alerts"),
        threading.Thread(target=guard_monitor_loop, name="guard"),
        threading.Thread(target=telegram_long_poll_loop, name="telegram"),
        threading.Thread(target=_scheduler_loop, name="scheduler"),
    ]
    for th in threads:
        th.start()
    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    finally:
        for th in threads:
            th.join(timeout=5)

if __name__=="__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    try:
        main()
    except Exception as e:
        log.exception("fatal: %s", e)
        try: send_telegram(f"💥 Fatal error: {escape_md(str(e))}")
        except: pass
        sys.exit(1)
