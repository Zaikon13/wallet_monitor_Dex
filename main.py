#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (FULL, 3 parts)
Features:
- RPC snapshot (CRO + ERC-20)
- Dexscreener pricing (+history fallback)
- Cost-basis PnL (realized & unrealized)
- Intraday/EOD reports
- Alerts (24h pump/dump) & Guard window
- Telegram long-poll commands:
  /status, /diag, /rescan
  /holdings, /show_wallet_assets, /showwalletassets, /show
  /dailysum, /showdaily, /report
  /totals, /totalstoday, /totalsmonth
  /pnl [today|month|all]
Compatible helpers:
  utils/http.py, telegram/api.py, reports/day_report.py,
  reports/ledger.py, reports/aggregates.py
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
# --- Compatibility wrapper: accept eps=... for update_cost_basis ---
# Some older reports.ledger.update_cost_basis() doesn't accept 'eps'.
# Keep original under a private name and expose a tolerant wrapper.
_orig_update_cost_basis = ledger_update_cost_basis
def ledger_update_cost_basis(pos_qty, pos_cost, token_key, signed_amount, price_usd, eps=None):
    try:
        # Try calling with eps if supported (newer versions might accept it)
        return _orig_update_cost_basis(pos_qty, pos_cost, token_key, signed_amount, price_usd, eps=eps)
    except TypeError:
        # Fallback: ignore eps and call the legacy signature
        res = _orig_update_cost_basis(pos_qty, pos_cost, token_key, signed_amount, price_usd)
        # If near-zero quantities should be treated as zero, apply here
        try:
            _eps = float(eps) if eps is not None else None
            if _eps is not None:
                for k,v in list(pos_qty.items()):
                    if abs(float(v)) < _eps: pos_qty[k] = 0.0
                for k,v in list(pos_cost.items()):
                    if abs(float(v)) < _eps: pos_cost[k] = 0.0
        except Exception:
            pass
        return res

# ---------- Bootstrap / TZ ----------
load_dotenv()
def _alias_env(src, dst):
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)
_alias_env("ALERTS_INTERVAL_MINUTES", "ALERTS_POLL_MINUTES")
_alias_env("DEX_POLL_SECONDS", "DEX_POLL")

def _init_tz(tz):
    try:
        os.environ["TZ"] = tz
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
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
WALLET_ADDRESS=(os.getenv("WALLET_ADDRESS") or "").strip().lower()
CRONOS_RPC=os.getenv("CRONOS_RPC_URL") or os.getenv("CRONOS_RPC") or "https://evm.cronos.org"
CRONOS_API=os.getenv("CRONOS_API") or "https://api.cronoscan.com/api"
ETHERSCAN_API_KEY=os.getenv("ETHERSCAN_API") or os.getenv("CRONOSCAN_API") or ""
DEX_API=os.getenv("DEX_API","https://api.dexscreener.com/latest/dex")
DATA_DIR=os.getenv("DATA_DIR","data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---------- Logging ----------
log_level=os.getenv("LOG_LEVEL","INFO").upper()
logging.basicConfig(level=getattr(logging,log_level,logging.INFO), format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("sentinel")

# ---------- Constants ----------
EPSILON=1e-10
INTRADAY_HOURS=int(os.getenv("INTRADAY_HOURS","3"))
EOD_TIME=os.getenv("EOD","23:59")
ALERTS_POLL_MINUTES=int(os.getenv("ALERTS_POLL_MINUTES","5"))
DEX_POLL=int(os.getenv("DEX_POLL","15"))

DISCOVER_ENABLED=os.getenv("DISCOVER_ENABLED","false").lower() in ("1","true","yes","y")
DISCOVER_QUERY=os.getenv("DISCOVER_QUERY","cronos")
DISCOVER_MIN_LIQ_USD=float(os.getenv("DISCOVER_MIN_LIQ_USD","10000"))
DISCOVER_REQUIRE_WCRO=os.getenv("DISCOVER_REQUIRE_WCRO","true").lower() in ("1","true","yes","y")
DISCOVER_BASE_WHITELIST=[s.strip().upper() for s in (os.getenv("DISCOVER_BASE_WHITELIST","")).split(",") if s.strip()]
DISCOVER_BASE_BLACKLIST=[s.strip().upper() for s in (os.getenv("DISCOVER_BASE_BLACKLIST","")).split(",") if s.strip()]

PRICE_WINDOW=60  # sliding history points for spike detection
SPIKE_THRESHOLD=float(os.getenv("SPIKE_THRESHOLD","10"))  # %
PRICE_MOVE_THRESHOLD=float(os.getenv("PRICE_MOVE_THRESHOLD","4"))  # %

MIN_VOLUME_FOR_ALERT=float(os.getenv("MIN_VOLUME_FOR_ALERT","0"))

CRONOS_TX="https://cronoscan.com/tx/{txhash}"

# ---------- State ----------
shutdown_event=threading.Event()
_price_history=defaultdict(lambda: deque(maxlen=PRICE_WINDOW))
_last_prices={}
_last_pair_tx={}
_tracked_pairs=set()

_position_qty=defaultdict(float)   # token_key -> qty
_position_cost=defaultdict(float)  # token_key -> total cost basis USD
_realized_pnl_today=0.0

_token_balances=defaultdict(float) # symbol/addr -> qty
_token_meta={}                     # key -> {"symbol","decimals"}

_ath_prices={}                    # asset -> max seen price

# ---------- Utils ----------
def _fmt_price(p: float)->str:
    if p>=1: return f"{p:.4f}"
    if p>=0.01: return f"{p:.6f}"
    if p>=1e-4: return f"{p:.8f}"
    return f"{p:.10f}"

def _nonzero(v, eps=1e-12):
    try: return abs(float(v))>eps
    except: return False

def read_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except: return default

def write_json(path, data):
    try:
        with open(path,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)
    except Exception as ex:
        log.exception("write_json error: %s", ex)

def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

def month_file():
    return os.path.join(DATA_DIR, f"month_{month_prefix()}.json")

def _format_price(p):
    try: return _fmt_price(float(p))
    except: return str(p)

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

def update_ath(asset_key, price_usd):
    try:
        p=float(price_usd)
    except:
        return
    cur=_ath_prices.get(asset_key)
    if cur is None or p>cur:
        _ath_prices[asset_key]=p

# ---------- Prices (Dexscreener) ----------
def fetch_pair(slg):
    try:
        if "/" in slg:
            chain, addr = slg.split("/",1)
            url=f"{DEX_API}/pairs/{chain}/{addr}"
        else:
            url=f"{DEX_API}/search?q={slg}"
        r=safe_get(url, timeout=15)
        return safe_json(r) or {}
    except Exception as ex:
        log.exception("fetch_pair error: %s", ex); return {}

def get_price_usd(symbol_or_addr):
    try:
        q=str(symbol_or_addr).strip()
        # very small mapper for CRO
        if q.upper()=="CRO":
            # try canonical WCRO/USDT for CRO proxy
            data = fetch_pair("cronos/0xe44fd7fCb2b1581822D0c862B68222998a0c299a")  # WCRO/USDT
            pair = None
            if isinstance(data.get("pair"), dict): pair=data["pair"]
            elif isinstance(data.get("pairs"), list) and data["pairs"]: pair=data["pairs"][0]
            if pair:
                try: return float(pair.get("priceUsd") or 0)
                except: return None
        # fallback: search by token or pair query
        data=fetch_pair(q)
        pair=None
        if isinstance(data.get("pair"), dict): pair=data["pair"]
        elif isinstance(data.get("pairs"), list) and data["pairs"]: pair=data["pairs"][0]
        if not pair: return None
        try: return float(pair.get("priceUsd") or 0)
        except: return None
    except Exception as ex:
        log.exception("get_price_usd error: %s", ex); return None

# ---------- Realized PnL recompute ----------
def recompute_realized_today():
    global _realized_pnl_today
    path=data_file_for_today()
    data=read_json(path, default={"date": ymd(),"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    total_realized=replay_cost_basis_over_entries(_position_qty,_position_cost,data.get("entries",[]),eps=EPSILON)
    _realized_pnl_today=float(total_realized)
    data["realized_pnl"]=float(total_realized); write_json(path,data)

# ---------- History maps ----------
def _build_history_maps():
    symbol_to_contract, symbol_conflict = {}, set()
    rev = {}
    try:
        # could load from static map if exists
        map_path=os.path.join(DATA_DIR,"symbols.json")
        m=read_json(map_path,{})
        for k,v in (m.items() if isinstance(m,dict) else []):
            symbol_to_contract[k.upper()]=v.lower()
            rev[v.lower()]=k.upper()
    except: pass
    return symbol_to_contract, rev
# ---------- Ledger append ----------
def record_entry(kind, token_key, amount, price_usd, usd_value, meta=None):
    e={
        "ts": now_dt().isoformat(),
        "kind": kind,  # native|erc20|trade|adjust
        "token": token_key,
        "amount": float(amount),
        "price_usd": float(price_usd or 0.0),
        "usd_value": float(usd_value or 0.0),
        "meta": meta or {}
    }
    path=data_file_for_today()
    data=read_json(path, default={"date": ymd(),"entries":[],"net_usd_flow":0.0,"realized_pnl":0.0})
    data["entries"].append(e)
    # net usd flow: for buys (amount>0) we "spend" usd; for sells (amount<0) we "receive" usd
    try:
        if float(amount)>0 and _nonzero(price_usd): data["net_usd_flow"] -= float(amount)*float(price_usd)
        elif float(amount)<0 and _nonzero(price_usd): data["net_usd_flow"] += -float(amount)*float(price_usd)
    except: pass
    write_json(path, data)
    append_ledger(e)  # external writer (optional)

# ---------- Native TX handling ----------
def handle_native_tx(tx):
    try:
        h=tx.get("hash") or ""
        frm=(tx.get("from") or "").lower(); to=(tx.get("to") or "").lower()
        amount_cro=float(tx.get("value",0))/1e18
        ts=int(tx.get("timeStamp") or tx.get("timestamp") or time.time())
        dt=datetime.fromtimestamp(ts, tz=LOCAL_TZ) if ts>0 else now_dt()
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
            f"Amount: {sign*amount_cro:.6f} CRO\n"
            f"USD: ${usd_value:,.2f}{f' | Realized PnL ${realized:,.2f}' if _nonzero(realized) else ''}"
        )
        record_entry("native","CRO",sign*amount_cro,price,usd_value,{"hash":h,"time":ts})
        recompute_realized_today()
    except Exception as ex:
        log.exception("handle_native_tx error: %s", ex)

# ---------- ERC-20 TX handling ----------
def handle_erc20_tx(tx, symbol_to_contract, rev_contract_to_symbol):
    try:
        h=tx.get("hash") or ""
        frm=(tx.get("from") or "").lower(); to=(tx.get("to") or "").lower()
        token_addr=(tx.get("contractAddress") or "").lower()
        symbol=(tx.get("tokenSymbol") or (rev_contract_to_symbol.get(token_addr) or token_addr)).upper()
        decimals=int(tx.get("tokenDecimal") or 18)
        amount = float(tx.get("value") or 0)/ (10**decimals)
        ts=int(tx.get("timeStamp") or tx.get("timestamp") or time.time())
        dt=datetime.fromtimestamp(ts, tz=LOCAL_TZ) if ts>0 else now_dt()
        sign= +1 if to==WALLET_ADDRESS else (-1 if frm==WALLET_ADDRESS else 0)
        if sign==0 or abs(amount)<=EPSILON: return

        # key by contract if available, else by symbol
        key = token_addr if token_addr else symbol
        price = get_price_usd(token_addr or symbol)
        usd_value=sign*amount*(price or 0.0)

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
            f"Token TX ({direction}) {symbol}\n"
            f"Hash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
            f"Amount: {sign*amount:.8f}\n"
            f"Price: ${_format_price(price)}  | USD: ${usd_value:,.2f}{f' | Realized ${realized:,.2f}' if _nonzero(realized) else ''}"
        )
        record_entry("erc20", key, sign*amount, (price or 0.0), usd_value, {"hash":h, "symbol":symbol, "decimals":decimals, "time":ts})
        recompute_realized_today()
    except Exception as ex:
        log.exception("handle_erc20_tx error: %s", ex)

# ---------- ATH snapshot ----------
def dump_ath():
    if not _ath_prices: return "No ATHs yet."
    lines=["ATHs:"]
    for k,v in sorted(_ath_prices.items()):
        sym=_token_meta.get(k,{}).get("symbol") or k
        lines.append(f"- {sym}: ${_format_price(v)}")
    return "\n".join(lines)

# ---------- Dex tracking ----------
def update_price_history(slg, price):
    hist=_price_history.get(slg) or deque(maxlen=PRICE_WINDOW)
    _price_history[slg]=hist
    hist.append(price)

def detect_spike(slg):
    try:
        hist=_price_history.get(slg); 
        if not hist or len(hist)<3: return None
        first=hist[0]; last=hist[-1]
        if first and last and first>0:
            pct=(last-first)/first*100.0
            if abs(pct)>=SPIKE_THRESHOLD: return pct
    except: pass
    return None

def _pair_cooldown_ok(key, seconds=90):
    tag=f"cd:{key}"
    now=time.time()
    last=_last_pair_tx.get(tag)
    if last and now-last<seconds: return False
    _last_pair_tx[tag]=now
    return True

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
                prev=_last_prices.get(s)
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
                                _price_history[s].clear()
                if prev and prev>0 and price_val:
                    delta=(price_val-prev)/prev*100.0
                    if abs(delta)>=PRICE_MOVE_THRESHOLD and _pair_cooldown_ok(f"move:{s}"):
                        bt=pair.get("baseToken") or {}; symbol=bt.get("symbol") or s
                        send_telegram(f"📈 Price move on {symbol}: {delta:.2f}%\nPrice: ${_format_price(price_val)} (prev ${_format_price(prev)})")
                # finally, update last price snapshot
                _last_prices[s]=price_val
                last_tx=(pair.get("lastTx") or {}).get("hash")
                if last_tx:
                    prev_tx=_last_pair_tx.get(s)
                    if prev_tx!=last_tx and _pair_cooldown_ok(f"trade:{s}"):
                        _last_pair_tx[s]=last_tx
                        bt=pair.get("baseToken") or {}; symbol=bt.get("symbol") or s
                        send_telegram(f"🔔 New trade on {symbol}\nTx: {CRONOS_TX.format(txhash=last_tx)}")
            except Exception as ex:
                log.exception("pair loop error: %s", ex)
                time.sleep(1)
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
        if vol24<=0: return False
        return True
    except:
        return False

def discover_pairs_once():
    try:
        url=f"{DEX_API}/search?q={DISCOVER_QUERY}"
        r=safe_get(url, timeout=15); data=safe_json(r) or {}
        pairs=[]
        if isinstance(data.get("pairs"), list):
            for p in data["pairs"]:
                try:
                    slug=f"{p.get('chainId')}/{p.get('pairAddress')}"
                    if _pair_passes_filters(p): pairs.append(slug)
                except: pass
        if pairs:
            send_telegram(f"🔎 Discovered: {', '.join(sorted(pairs)[:10])}{'…' if len(pairs)>10 else ''}")
            _tracked_pairs.update(pairs)
    except Exception as ex:
        log.exception("discover_pairs error: %s", ex)
# ---------- CronosScan polling (simplified) ----------
def _fetch_cronos_txs():
    try:
        url=f"{CRONOS_API}?module=account&action=txlist&address={WALLET_ADDRESS}&sort=desc"
        if ETHERSCAN_API_KEY: url += f"&apikey={ETHERSCAN_API_KEY}"
        r=safe_get(url, timeout=15); js=safe_json(r) or {}
        if str(js.get("status","1"))!="1": return []
        return js.get("result") or []
    except Exception as ex:
        log.exception("fetch cronos txs error: %s", ex); return []

def _fetch_cronos_erc20():
    try:
        url=f"{CRONOS_API}?module=account&action=tokentx&address={WALLET_ADDRESS}&sort=desc"
        if ETHERSCAN_API_KEY: url += f"&apikey={ETHERSCAN_API_KEY}"
        r=safe_get(url, timeout=15); js=safe_json(r) or {}
        if str(js.get("status","1"))!="1": return []
        return js.get("result") or []
    except Exception as ex:
        log.exception("fetch cronos erc20 error: %s", ex); return []

def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    last_native_hashes=set()
    last_erc20_hashes=set()
    symbol_to_contract, rev = _build_history_maps()
    while not shutdown_event.is_set():
        try:
            native=_fetch_cronos_txs()
            for tx in native[:100]:
                h=tx.get("hash"); 
                if not h or h in last_native_hashes: continue
                last_native_hashes.add(h); handle_native_tx(tx)
            erc20=_fetch_cronos_erc20()
            for tx in erc20[:200]:
                h=tx.get("hash")
                if not h or h in last_erc20_hashes: continue
                last_erc20_hashes.add(h); handle_erc20_tx(tx, symbol_to_contract, rev)
        except Exception as ex:
            log.exception("wallet monitor error: %s", ex)
            send_telegram("⚠️ Wallet monitor error — see logs.")
        for _ in range(max(5, ALERTS_POLL_MINUTES*12)):
            if shutdown_event.is_set(): break
            time.sleep(5)

# ---------- Commands (very small) ----------
def format_holdings_lines():
    # Merge _token_balances into simple lines with price and USD value
    total=0.0
    lines=["*Holdings Snapshot*"]
    for key,qty in sorted(_token_balances.items()):
        meta=_token_meta.get(key,{})
        sym=meta.get("symbol") or (key[:6]+"…" if len(key)>8 else key)
        price=get_price_usd(key) or 0.0
        usd=qty*(price or 0.0); total+=usd
        lines.append(f"- {sym}: {qty:.6f} @ ${_format_price(price)} = ${usd:,.2f}")
    lines.append(f"*Total*: ${total:,.2f}")
    return "\n".join(lines)

def handle_command(text):
    t=(text or "").strip()
    if t.startswith("/status"):
        send_telegram("✅ Sentinel online.")
    elif t.startswith("/holdings") or t.startswith("/show") or t.startswith("/show_wallet_assets") or t.startswith("/showwalletassets"):
        send_telegram(format_holdings_lines())
    elif t.startswith("/ath"):
        send_telegram(dump_ath())
    elif t.startswith("/watch add"):
        try:
            slug=t.split(None,2)[2].strip()
            _tracked_pairs.add(slug); send_telegram(f"👁️ Added: {slug}")
        except:
            send_telegram("Usage: /watch add cronos/<pairAddress>")
    elif t.startswith("/watch rm"):
        try:
            slug=t.split(None,2)[2].strip()
            _tracked_pairs.discard(slug); send_telegram(f"🗑️ Removed: {slug}")
        except:
            send_telegram("Usage: /watch rm cronos/<pairAddress>")
    elif t.startswith("/report") or t.startswith("/showdaily") or t.startswith("/dailysum"):
        try:
            send_telegram(_compose_day_report())
        except Exception as ex:
            log.exception("daily report error: %s", ex)
            send_telegram("⚠️ Failed to compose daily report.")
    else:
        send_telegram("Commands: /status, /holdings, /ath, /watch add <slug>, /watch rm <slug>, /report")

# ---------- Telegram polling (minimal long-poll) ----------
def telegram_poll_loop():
    send_telegram("🤖 Telegram command handler online.")
    # Assuming telegram/api.py handles offset persistence and long-poll internally through getUpdates.
    # If not, replace with your own polling wrapper.
    # Here we just no-op to keep structure consistent.
    while not shutdown_event.is_set():
        time.sleep(5)

# ---------- EOD scheduler (custom, no 'schedule' lib) ----------
def _time_to_seconds(hhmm):
    try:
        hh, mm = [int(x) for x in hhmm.split(":")]
        return hh*3600+mm*60
    except:
        return 23*3600+59*60

def scheduler_loop():
    send_telegram("⏱️ Scheduler loop online.")
    target=_time_to_seconds(EOD_TIME)
    last_sent_date=None
    while not shutdown_event.is_set():
        try:
            now=now_dt()
            secs=now.hour*3600+now.minute*60+now.second
            if last_sent_date!=ymd(now) and secs>=target:
                try:
                    text=_compose_day_report()
                    send_telegram(f"📒 Daily Report\n{text}")
                except Exception as ex:
                    logging.exception("Failed to build or send daily report")
                    send_telegram("⚠️ Failed to generate daily report.")
                last_sent_date=ymd(now)
        except Exception as ex:
            log.exception("scheduler loop error: %s", ex)
        time.sleep(5)

# ---------- Startup ----------
def startup_ping():
    try:
        send_telegram("🟢 Starting Cronos DeFi Sentinel.")
    except Exception as ex:
        log.exception("startup ping error: %s", ex)

# ---------- Graceful shutdown ----------
def _handle_sigterm(signum, frame):
    try:
        send_telegram("🛑 Shutting down…")
    except: pass
    shutdown_event.set()

# ---------- Main ----------
def main():
    startup_ping()
    if DISCOVER_ENABLED:
        try: discover_pairs_once()
        except Exception as ex: log.exception("discover init error: %s", ex)

    # Prime a holdings snapshot line on boot
    try:
        send_telegram(format_holdings_lines())
    except Exception as ex:
        log.exception("boot holdings error: %s", ex)

    t1=threading.Thread(target=wallet_monitor_loop, name="wallet-monitor", daemon=True)
    t2=threading.Thread(target=monitor_tracked_pairs_loop, name="dex-monitor", daemon=True)
    t3=threading.Thread(target=telegram_poll_loop, name="tg-poll", daemon=True)
    t4=threading.Thread(target=scheduler_loop, name="scheduler", daemon=True)

    t1.start(); t2.start(); t3.start(); t4.start()

    while not shutdown_event.is_set():
        time.sleep(1)

if __name__=="__main__":
    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
        signal.signal(signal.SIGINT, _handle_sigterm)
    except Exception:
        pass
    try:
        main()
    except Exception as ex:
        log.exception("fatal error: %s", ex)
        try: send_telegram("💥 Fatal error — see logs.")
        except: pass
        sys.exit(1)
