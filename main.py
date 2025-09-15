# wallet\_monitor\_Dex — Modularized Repo (v1)

> **Tree (as requested)**
>
> ```
> .
> .github
> core
>   ├─ __init__.py
>   ├─ config.py
>   ├─ holdings.py
>   ├─ pricing.py
>   ├─ rpc.py
>   ├─ tz.py
>   └─ watch.py
> reports
>   ├─ __init__.py
>   ├─ aggregates.py
>   ├─ day_report.py
>   └─ ledger.py
> telegram
>   ├─ __init__.py
>   ├─ api.py
>   └─ formatters.py
> tests
> utils
>   ├─ __init__.py
>   └─ http.py
> .env
> Procfile
> README.md
> SUMMARY.md
> __init__.py
> main.py
> requirements.txt
> setup.cfg
> ```
>
> **Σημείωση:** Κράτησα τις βασικές λειτουργίες του παλιού `main.py` (RPC snapshot, Dexscreener pricing, cost‑basis PnL, Intraday/EOD, Alerts, Guard, Telegram commands), αλλά τις μοίρασα σε modules. Το `main.py` παραμένει <1000 γραμμές.

---

## core/**init**.py

```python
# Empty on purpose (package marker)
```

## core/tz.py

```python
from datetime import datetime
from zoneinfo import ZoneInfo
import os, time

_DEF_TZ = os.getenv("TZ", "Europe/Athens")

def tz_init(tz_str: str | None = None) -> ZoneInfo:
    tz = tz_str or _DEF_TZ
    os.environ["TZ"] = tz
    if hasattr(time, "tzset"):  # type: ignore
        try:
            time.tzset()  # type: ignore[attr-defined]
        except Exception:
            pass
    return ZoneInfo(tz)

LOCAL_TZ = tz_init(_DEF_TZ)

def now_dt():
    return datetime.now(LOCAL_TZ)

def ymd(dt=None):
    return (dt or now_dt()).strftime("%Y-%m-%d")

def month_prefix(dt=None):
    return (dt or now_dt()).strftime("%Y-%m")
```

## core/config.py

```python
import os
from dataclasses import dataclass

# Map legacy envs → new names (backwards compatibility)
_ALIAS = {
    "ALERTS_INTERVAL_MINUTES": "ALERTS_INTERVAL_MIN",
    "DISCOVER_REQUIRE_WCRO_QUOTE": "DISCOVER_REQUIRE_WCRO",
}

for src, dst in _ALIAS.items():
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)  # noqa: F401

@dataclass(frozen=True)
class Settings:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    WALLET_ADDRESS: str = (os.getenv("WALLET_ADDRESS", "")).lower()
    ETHERSCAN_API: str = os.getenv("ETHERSCAN_API", "")
    CRONOS_RPC_URL: str = os.getenv("CRONOS_RPC_URL", "")

    TOKENS: str = os.getenv("TOKENS", "")
    DEX_PAIRS: str = os.getenv("DEX_PAIRS", "")

    LOG_SCAN_BLOCKS: int = int(os.getenv("LOG_SCAN_BLOCKS", "120000"))
    LOG_SCAN_CHUNK: int  = int(os.getenv("LOG_SCAN_CHUNK",  "5000"))
    WALLET_POLL: int     = int(os.getenv("WALLET_POLL", "15"))
    DEX_POLL: int        = int(os.getenv("DEX_POLL", "60"))
    PRICE_WINDOW: int    = int(os.getenv("PRICE_WINDOW","3"))

    PRICE_MOVE_THRESHOLD: float = float(os.getenv("PRICE_MOVE_THRESHOLD","5"))
    SPIKE_THRESHOLD: float      = float(os.getenv("SPIKE_THRESHOLD","8"))
    MIN_VOLUME_FOR_ALERT: float = float(os.getenv("MIN_VOLUME_FOR_ALERT","0"))

    DISCOVER_ENABLED: bool = os.getenv("DISCOVER_ENABLED","true").lower() in ("1","true","yes","on")
    DISCOVER_QUERY: str = os.getenv("DISCOVER_QUERY","cronos")
    DISCOVER_LIMIT: int = int(os.getenv("DISCOVER_LIMIT","10"))
    DISCOVER_POLL: int = int(os.getenv("DISCOVER_POLL","120"))
    DISCOVER_MIN_LIQ_USD: float = float(os.getenv("DISCOVER_MIN_LIQ_USD","30000"))
    DISCOVER_MIN_VOL24_USD: float = float(os.getenv("DISCOVER_MIN_VOL24_USD","5000"))
    DISCOVER_MIN_ABS_CHANGE_PCT: float = float(os.getenv("DISCOVER_MIN_ABS_CHANGE_PCT","10"))
    DISCOVER_MAX_PAIR_AGE_HOURS: int = int(os.getenv("DISCOVER_MAX_PAIR_AGE_HOURS","24"))
    DISCOVER_REQUIRE_WCRO: bool = os.getenv("DISCOVER_REQUIRE_WCRO","false").lower() in ("1","true","yes","on")
    DISCOVER_BASE_WHITELIST: list[str] = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_WHITELIST","").split(",") if s.strip()]
    DISCOVER_BASE_BLACKLIST: list[str] = [s.strip().upper() for s in os.getenv("DISCOVER_BASE_BLACKLIST","").split(",") if s.strip()]

    INTRADAY_HOURS: int = int(os.getenv("INTRADAY_HOURS","3"))
    EOD_HOUR: int = int(os.getenv("EOD_HOUR","23"))
    EOD_MINUTE: int = int(os.getenv("EOD_MINUTE","59"))

    ALERTS_INTERVAL_MIN: int = int(os.getenv("ALERTS_INTERVAL_MIN","15"))
    DUMP_ALERT_24H_PCT: float = float(os.getenv("DUMP_ALERT_24H_PCT","-15"))
    PUMP_ALERT_24H_PCT: float = float(os.getenv("PUMP_ALERT_24H_PCT","20"))

    GUARD_WINDOW_MIN: int = int(os.getenv("GUARD_WINDOW_MIN","60"))
    GUARD_PUMP_PCT: float = float(os.getenv("GUARD_PUMP_PCT","20"))
    GUARD_DROP_PCT: float = float(os.getenv("GUARD_DROP_PCT","-12"))
    GUARD_TRAIL_DROP_PCT: float = float(os.getenv("GUARD_TRAIL_DROP_PCT","-8"))

    RECEIPT_SYMBOLS: set[str] = set([s.strip().upper() for s in os.getenv("RECEIPT_SYMBOLS","TCRO").split(",") if s.strip()])

    DATA_DIR: str = os.getenv("DATA_DIR", "/app/data")

settings = Settings()
```

## utils/**init**.py

```python
# Empty on purpose
```

## utils/http.py

```python
import requests, time

_DEF_HEADERS = {"User-Agent": "wallet-monitor/1.0"}

def safe_get(url: str, params: dict | None = None, timeout: int = 15, retries: int = 2):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=_DEF_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
        except Exception as e:
            last = e
        time.sleep(0.4 * (i + 1))
    return None

def safe_json(resp):
    try:
        if resp is None: return None
        return resp.json()
    except Exception:
        return None
```

## core/pricing.py

```python
from collections import deque
from typing import Any
import time
from utils.http import safe_get, safe_json

DEX_BASE_PAIRS   = "https://api.dexscreener.com/latest/dex/pairs"
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"

PRICE_ALIASES = {"tcro":"cro"}
PRICE_CACHE: dict[str, tuple[float | None, float]] = {}
PRICE_CACHE_TTL = 60
_HISTORY_LAST_PRICE: dict[str, float] = {}

_price_history: dict[str, deque] = {}

def pick_best_price(pairs: list[dict[str,Any]] | None):
    if not pairs: return None
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")) != "cronos" and str(p.get("chainId","")).lower() != "cronos":
                continue
            liq=float((p.get("liquidity") or {}).get("usd") or 0)
            price=float(p.get("priceUsd") or 0)
            if price<=0: continue
            if liq>best_liq:
                best_liq, best = liq, price
        except:  # noqa: E722
            continue
    return best

def pairs_for_token_addr(addr: str):
    data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/cronos/{addr}", timeout=10)) or {}
    pairs = data.get("pairs") or []
    if not pairs:
        data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/{addr}", timeout=10)) or {}
        pairs = data.get("pairs") or []
    if not pairs:
        data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": addr}, timeout=10)) or {}
        pairs = data.get("pairs") or []
    return pairs

def history_price_fallback(query_key: str, symbol_hint: str | None = None):
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
    if sym=="CRO":  # explicit CRO
        p=_HISTORY_LAST_PRICE.get("CRO")
        if p and p>0: return p
    return None

def price_cro_fallback():
    for q in ["wcro usdc","cro usdc","cro busd","cro dai"]:
        data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
        p=pick_best_price(data.get("pairs"))
        if p and p>0: return p
    return None

def get_price_usd(symbol_or_addr: str):
    if not symbol_or_addr: return None
    key = PRICE_ALIASES.get(symbol_or_addr.strip().lower(), symbol_or_addr.strip().lower())
    now = time.time()
    c = PRICE_CACHE.get(key)
    if c and (now - c[1] < PRICE_CACHE_TTL):
        return c[0]

    price=None
    try:
        if key in ("cro","wcro","w-cro","wrappedcro","wrapped cro"):
            for q in ["wcro usdt","cro usdt"]:
                data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
                price=pick_best_price(data.get("pairs"))
                if price: break
            if not price:
                price=price_cro_fallback()
        elif key.startswith("0x") and len(key)==42:
            price=pick_best_price(pairs_for_token_addr(key))
        else:
            for q in [key, f"{key} usdt", f"{key} wcro"]:
                data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
                price=pick_best_price(data.get("pairs"))
                if price: break
    except:  # noqa: E722
        price=None

    if (price is None) or (not price) or (float(price)<=0):
        hist=history_price_fallback(symbol_or_addr, symbol_hint=symbol_or_addr)
        if hist and hist>0: price=float(hist)

    PRICE_CACHE[key]=(price, now)
    return price

def get_change_and_price_for_symbol_or_addr(sym_or_addr: str):
    if sym_or_addr.lower().startswith("0x") and len(sym_or_addr)==42:
        pairs=pairs_for_token_addr(sym_or_addr)
    else:
        data=safe_json(safe_get(DEX_BASE_SEARCH, params={"q": sym_or_addr}, timeout=10)) or {}
        pairs=data.get("pairs") or []
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId","")) not in ("cronos", "25") and str(p.get("chainId","")).lower()!="cronos":
                continue
            liq=float((p.get("liquidity") or {}).get("usd") or 0)
            price=float(p.get("priceUsd") or 0)
            if price<=0: continue
            if liq>best_liq: best_liq, best = liq, p
        except:  # noqa: E722
            continue
    if not best:
        return (None,None,None,None)
    price=float(best.get("priceUsd") or 0)
    ch=best.get("priceChange") or {}
    ch24=ch2h=None
    try:
        if "h24" in ch: ch24=float(ch.get("h24"))
        if "h2" in ch: ch2h=float(ch.get("h2"))
    except:  # noqa: E722
        pass
    ds_url=f"https://dexscreener.com/cronos/{best.get('pairAddress')}"
    return (price, ch24, ch2h, ds_url)

# expose history map so holdings can seed it
HISTORY_LAST_PRICE = _HISTORY_LAST_PRICE
PRICE_HISTORY = _price_history
```

## core/rpc.py

```python
from collections import deque
from typing import Iterable
from web3 import Web3
from .tz import now_dt

TRANSFER_TOPIC0="0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

ERC20_ABI_MIN=[
    {"constant":True,"inputs":[],"name":"name","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"type":"function"},
    {"constant":True,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"},
    {"constant":True,"inputs":[{"name":"owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"type":"function"},
]

class CronosRPC:
    def __init__(self, rpc_url: str):
        self.web3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout":15}))
        self.sym_cache: dict[str,str] = {}
        self.dec_cache: dict[str,int] = {}

    def is_connected(self) -> bool:
        try:
            return self.web3.is_connected()
        except Exception:
            return False

    def to_checksum(self, addr: str) -> str:
        try:
            return Web3.to_checksum_address(addr)
        except Exception:
            return addr

    def block_number(self) -> int | None:
        try:
            return self.web3.eth.block_number
        except Exception:
            return None

    def get_native_balance(self, addr: str) -> float:
        try:
            wei=self.web3.eth.get_balance(self.to_checksum(addr))
            return float(wei)/(10**18)
        except Exception:
            return 0.0

    def get_symbol_decimals(self, contract: str) -> tuple[str,int]:
        if contract in self.sym_cache and contract in self.dec_cache:
            return self.sym_cache[contract], self.dec_cache[contract]
        try:
            c=self.web3.eth.contract(address=self.to_checksum(contract), abi=ERC20_ABI_MIN)
            sym=c.functions.symbol().call(); dec=int(c.functions.decimals().call())
            self.sym_cache[contract]=sym; self.dec_cache[contract]=dec
            return sym,dec
        except Exception:
            self.sym_cache[contract]=contract[:8].upper(); self.dec_cache[contract]=18
            return self.sym_cache[contract], self.dec_cache[contract]

    def get_erc20_balance(self, contract: str, owner: str) -> float:
        try:
            c=self.web3.eth.contract(address=self.to_checksum(contract), abi=ERC20_ABI_MIN)
            bal=c.functions.balanceOf(self.to_checksum(owner)).call()
            _,dec=self.get_symbol_decimals(contract)
            return float(bal)/(10**dec)
        except Exception:
            return 0.0

    def discover_token_contracts_by_logs(self, owner: str, start_block: int, end_block: int, chunk: int = 5_000) -> set[str]:
        if start_block<0: start_block=0
        if end_block<start_block: return set()
        wallet_topic="0x"+"0"*24+self.to_checksum(owner).lower().replace("0x","")
        found:set[str]=set()
        frm=start_block
        while frm<=end_block:
            to=min(end_block, frm+chunk-1)
            for topics in [[TRANSFER_TOPIC0, wallet_topic],[TRANSFER_TOPIC0,None,wallet_topic]]:
                try:
                    logs=self.web3.eth.get_logs({"fromBlock":frm,"toBlock":to,"topics":topics})
                    for lg in logs:
                        addr=(lg.get("address") or "").lower()
                        if addr.startswith("0x"): found.add(addr)
                except Exception:
                    pass
            frm=to+1
        return found
```

## core/holdings.py

```python
from collections import defaultdict
import os, json
from .tz import ymd
from .pricing import get_price_usd, HISTORY_LAST_PRICE

EPSILON=1e-12

DATA_DIR = os.getenv("DATA_DIR", "/app/data")

def read_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except Exception:
        return default

# Rebuild open positions from historical ledger files (cost-basis)
# entries must be append_ledger schema compatible

def rebuild_open_positions_from_history(data_dir: str = DATA_DIR):
    pos_qty, pos_cost = defaultdict(float), defaultdict(float)

    files=[]
    try:
        for fn in os.listdir(data_dir):
            if fn.startswith("transactions_") and fn.endswith(".json"): files.append(fn)
    except Exception:
        pass
    files.sort()

    # symbol → last seen contract (ignore conflicts)
    sym2addr, conflict=set(),{}

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

    for fn in files:
        data=read_json(os.path.join(data_dir,fn), default=None)
        if not isinstance(data,dict): continue
        for e in data.get("entries",[]):
            sym=(e.get("token") or "").strip()
            addr=(e.get("token_addr") or "").strip().lower()
            amt=float(e.get("amount") or 0.0)
            pr=float(e.get("price_usd") or 0.0)
            if addr and addr.startswith("0x"):
                key=addr
            else:
                key=sym.upper() or sym
            _update(pos_qty,pos_cost,key,amt,pr)
            # seed history prices for fallbacks
            if pr>0:
                if addr: HISTORY_LAST_PRICE[addr]=pr
                if sym: HISTORY_LAST_PRICE[sym.upper()]=pr

    for k,v in list(pos_qty.items()):
        if abs(v)<1e-10: pos_qty[k]=0.0
    return pos_qty, pos_cost


def compute_holdings_from_positions(pos_qty: dict, pos_cost: dict):
    total, breakdown, unrealized = 0.0, [], 0.0

    def _sym_for_key(key):
        if isinstance(key,str) and key.startswith("0x"):
            return key[:8].upper()
        return str(key)

    for key,amt in pos_qty.items():
        amt=max(0.0,float(amt))
        if amt<=EPSILON: continue
        sym=_sym_for_key(key)
        p=(get_price_usd(key) if (isinstance(key,str) and key.startswith("0x")) else get_price_usd(sym)) or 0.0
        if (not p) or p<=0:
            p=(get_price_usd(sym) or 0.0)
        v=amt*(p or 0.0)
        total+=v
        breakdown.append({"token":sym,"token_addr": key if (isinstance(key,str) and key.startswith("0x")) else None,
                          "amount":amt,"price_usd":p,"usd_value":v})
        cost=pos_cost.get(key,0.0)
        if amt>EPSILON and p>0: unrealized += (amt*p - cost)

    breakdown.sort(key=lambda b: float(b.get("usd_value",0.0)), reverse=True)
    return total, breakdown, unrealized
```

## core/watch.py

```python
from collections import deque

class WatchList:
    def __init__(self):
        self.tracked:set[str]=set()
        self.last_price:dict[str,float|None]={}
        self.price_window:dict[str,deque]={}

    def ensure(self, slug: str, window: int = 3):
        if slug in self.tracked: return False
        self.tracked.add(slug)
        self.last_price[slug]=None
        self.price_window[slug]=deque(maxlen=window)
        return True

    def push_price(self, slug: str, price: float):
        if slug not in self.tracked: return
        self.price_window[slug].append(price)
        self.last_price[slug]=price

    def detect_spike(self, slug: str, threshold_pct: float):
        hist=self.price_window.get(slug)
        if not hist or len(hist)<2: return None
        first,last = hist[0], hist[-1]
        if not first: return None
        pct=(last-first)/first*100.0
        return pct if abs(pct)>=threshold_pct else None
```

## reports/**init**.py

```python
# Empty on purpose
```

## reports/aggregates.py

```python
from collections import defaultdict

def aggregate_per_asset(rows):
    agg=defaultdict(lambda: {"asset":"?","in_qty":0.0,"out_qty":0.0,"in_usd":0.0,"out_usd":0.0,"realized_usd":0.0})
    for r in rows or []:
        a=r.get("asset") or "?"; side=r.get("side") or "?"
        x=agg[a]; x["asset"]=a
        qty=float(r.get("qty") or 0.0)
        usd=float(r.get("usd") or 0.0)
        realized=float(r.get("realized_usd") or 0.0)
        if side=="IN":
            x["in_qty"]+=qty; x["in_usd"]+=usd
        else:
            x["out_qty"]+=qty; x["out_usd"]+=-abs(usd) if usd<0 else usd
        x["realized_usd"]+=realized
    return list(agg.values())
```

## reports/day\_report.py

```python
from .ledger import data_file_for_today, read_json
from .aggregates import aggregate_per_asset
from core.tz import ymd

_DEF_HEADER = "Daily Report"

def build_day_report_text(*, date_str: str, entries, net_flow: float, realized_today_total: float, holdings_total: float, breakdown: list, unrealized: float, data_dir: str) -> str:
    lines=[f"📒 {_DEF_HEADER} ({date_str})"]
    if not entries:
        lines.append("No transactions today.")
    for e in entries or []:
        sym=(e.get("token") or "?")
        amt=float(e.get("amount") or 0.0)
        pr=float(e.get("price_usd") or 0.0)
        usd=float(e.get("usd_value") or 0.0)
        lines.append(f"• {sym}: {amt:.6f} @ ${pr:.6f} = ${usd:.2f}")
    lines.append("")
    lines.append(f"Net USD flow today: ${net_flow:.8f}")
    lines.append(f"Realized PnL today: ${realized_today_total:.8f}")
    lines.append(f"Holdings (MTM) now: ${holdings_total:.4f}")
    for b in breakdown or []:
        lines.append(f"  – {b['token']}: {b['amount']:.4f} @ ${b['price_usd']:.6f} = ${b['usd_value']:.4f}")
    if unrealized:
        lines.append(f"Unrealized (open): ${unrealized:.4f}")
    return "\n".join(lines)
```

## reports/ledger.py

```python
import os, json, time
from core.tz import ymd

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
EPSILON=1e-12

os.makedirs(DATA_DIR, exist_ok=True)

def read_json(path, default):
    try:
        with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except Exception:
        return default

def write_json(path, obj):
    tmp=path+".tmp"
    with open(tmp,"w",encoding="utf-8") as f: json.dump(obj,f,ensure_ascii=False,indent=2)
    os.replace(tmp,path)

def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

# Cost-basis helpers (FIFO avg cost per asset key)

def update_cost_basis(pos_qty: dict, pos_cost: dict, token_key: str, signed_amount: float, price_usd: float, eps: float = EPSILON) -> float:
    realized=0.0
    qty=pos_qty.get(token_key,0.0)
    cost=pos_cost.get(token_key,0.0)
    if signed_amount>eps:
        pos_qty[token_key]=qty+signed_amount
        pos_cost[token_key]=cost+signed_amount*(price_usd or 0.0)
    elif signed_amount<-eps and qty>eps:
        sell_qty=min(-signed_amount, qty)
        avg_cost=(cost/qty) if qty>eps else (price_usd or 0.0)
        realized=(price_usd-avg_cost)*sell_qty
        pos_qty[token_key]=qty-sell_qty
        pos_cost[token_key]=max(0.0, cost - avg_cost*sell_qty)
    return realized


def replay_cost_basis_over_entries(pos_qty: dict, pos_cost: dict, entries: list, eps: float = EPSILON) -> float:
    total_realized=0.0
    for e in entries or []:
        key=(e.get("token_addr") or "").lower() or (e.get("token") or "").upper()
        amt=float(e.get("amount") or 0.0)
        pr=float(e.get("price_usd") or 0.0)
        total_realized += update_cost_basis(pos_qty,pos_cost,key,amt,pr,eps)
    return total_realized


def append_ledger(entry: dict):
    path=data_file_for_today()
    data=read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    data["entries"].append(entry)
    data["net_usd_flow"]=float(data.get("net_usd_flow",0.0))+float(entry.get("usd_value") or 0.0)
    data["realized_pnl"]=float(data.get("realized_pnl",0.0))+float(entry.get("realized_pnl") or 0.0)
    write_json(path, data)
```

## telegram/**init**.py

```python
# Empty on purpose
```

## telegram/api.py

```python
import os, requests

BOT=os.getenv("TELEGRAM_BOT_TOKEN","")
CHAT=os.getenv("TELEGRAM_CHAT_ID","")

def send_telegram(text: str):
    if not BOT or not CHAT or not text:
        return
    url=f"https://api.telegram.org/bot{BOT}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT, "text": text, "parse_mode": "Markdown"}, timeout=15)
    except Exception:
        pass
```

## telegram/formatters.py

```python
from core.tz import ymd

def fmt_amount(a: float) -> str:
    a=float(a)
    if abs(a)>=1: return f"{a:,.4f}"
    if abs(a)>=0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

```

## tests (placeholder)

```text
(You can add unit tests later; kept empty for now.)
```

## .github/workflows/ci.yml (placeholder)

```yaml
name: ci
on: [push]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install ruff
      - run: ruff check .
```

## README.md

````markdown
# Cronos DeFi Sentinel (Modular v1)

Modularized structure extracted from your monolithic `main.py`.
- RPC snapshot, Dexscreener pricing
- Cost-basis PnL (realized & unrealized)
- Intraday/EOD reports
- Alerts (pump/dump) & Guard
- Telegram commands

**Run:**
```bash
python3 main.py
````

````

## SUMMARY.md
```markdown
- `core/`: timezone, config, pricing (Dexscreener), rpc (Web3), holdings helpers, watchlist.
- `reports/`: ledger append/replay, day report, aggregates.
- `telegram/`: send_telegram + formatters.
- `utils/`: safe HTTP helpers.
- `main.py`: orchestration (threads: wallet, dex monitor, alerts, guard, scheduler, telegram commands).
````

## Procfile

```text
worker: python3 main.py
```

## requirements.txt

```text
python-dotenv
requests
web3
```

## setup.cfg

```ini
[ruff]
line-length = 100
select = ["E","F","W"]
ignore = ["E501"]
```

## **init**.py (repo root)

```python
__all__ = []
```

## .env (template)

```env
TZ=Europe/Athens
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_CHAT_ID=5307877340
WALLET_ADDRESS=0xea53d79ce2a915033e6b4c5ebe82bb6b292e35cc
ETHERSCAN_API=your_key
CRONOS_RPC_URL=https://cronos.node
```

## main.py

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cronos DeFi Sentinel — Modular main
Keeps <1000 lines; orchestrates modules.
"""
from __future__ import annotations

import os, time, json, signal, logging, threading
from collections import defaultdict, deque
from datetime import datetime

from dotenv import load_dotenv

from core.config import settings
from core.tz import tz_init, now_dt, ymd
from core.pricing import get_price_usd, get_change_and_price_for_symbol_or_addr
from core.rpc import CronosRPC
from core.holdings import rebuild_open_positions_from_history, compute_holdings_from_positions
from reports.ledger import append_ledger, replay_cost_basis_over_entries, data_file_for_today, read_json
from reports.day_report import build_day_report_text
from reports.aggregates import aggregate_per_asset
from telegram.api import send_telegram

load_dotenv()
os.makedirs(settings.DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log=logging.getLogger("main")

# --- Globals (runtime state) ---
shutdown_event=threading.Event()
LOCAL_TZ=tz_init(os.getenv("TZ","Europe/Athens"))

position_qty=defaultdict(float)
position_cost=defaultdict(float)
EPS=1e-12

_seen_native=set()
_seen_token=set()

# --- Etherscan lite (HTTP v2) ---
ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"
CRONOS_CHAINID   = 25
CRONOS_TX        = "https://cronoscan.com/tx/{txhash}"

from utils.http import safe_get, safe_json

def fetch_latest_wallet_txs(limit=25):
    if not settings.WALLET_ADDRESS or not settings.ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"txlist","address":settings.WALLET_ADDRESS,
            "startblock":0,"endblock":99999999,"page":1,"offset":limit,"sort":"desc","apikey":settings.ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status",""))=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

def fetch_latest_token_txs(limit=100):
    if not settings.WALLET_ADDRESS or not settings.ETHERSCAN_API: return []
    params={"chainid":CRONOS_CHAINID,"module":"account","action":"tokentx","address":settings.WALLET_ADDRESS,
            "startblock":0,"endblock":99999999,"page":1,"offset":limit,"sort":"desc","apikey":settings.ETHERSCAN_API}
    data=safe_json(safe_get(ETHERSCAN_V2_URL, params=params, timeout=15, retries=3)) or {}
    if str(data.get("status",""))=="1" and isinstance(data.get("result"), list): return data["result"]
    return []

# --- Cost-basis replay for today ---

def replay_today_cost_basis():
    position_qty.clear(); position_cost.clear()
    data=read_json(data_file_for_today(), default={"date": ymd(), "entries": []})
    total=replay_cost_basis_over_entries(position_qty, position_cost, data.get("entries",[]), eps=EPS)
    return total

# --- Holdings ---

rpc = CronosRPC(settings.CRONOS_RPC_URL) if settings.CRONOS_RPC_URL else None

def compute_holdings_merged():
    # History-based positions (rebuild)
    pos_qty, pos_cost = rebuild_open_positions_from_history(settings.DATA_DIR)

    # Add on-chain CRO + discovered ERC-20 balances
    total, breakdown, unreal = compute_holdings_from_positions(pos_qty, pos_cost)
    return total, breakdown, unreal

# --- Native / ERC-20 handlers ---

def _fmt_amount(a: float) -> str:
    a=float(a)
    if abs(a)>=1: return f"{a:,.4f}"
    if abs(a)>=0.0001: return f"{a:.6f}"
    return f"{a:.8f}"

def _fmt_price(p: float) -> str:
    p=float(p)
    if p>=1: return f"{p:,.6f}"
    if p>=0.01: return f"{p:.6f}"
    if p>=1e-6: return f"{p:.8f}"
    return f"{p:.10f}"


def handle_native_tx(tx: dict):
    h=tx.get("hash");
    if not h or h in _seen_native: return
    _seen_native.add(h)
    val_raw=tx.get("value","0")
    try: amount_cro=int(val_raw)/(10**18)
    except: amount_cro=float(val_raw)
    frm=(tx.get("from") or "").lower(); to=(tx.get("to") or "").lower()
    ts=int(tx.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts, LOCAL_TZ)
    sign= +1 if to==settings.WALLET_ADDRESS else (-1 if frm==settings.WALLET_ADDRESS else 0)
    if sign==0 or abs(amount_cro)<=EPS: return
    price=get_price_usd("CRO") or 0.0
    usd_value=sign*amount_cro*price

    realized = 0.0  # will be recomputed in replay_today_cost_basis()
    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"), "txhash": h, "type":"native",
        "token":"CRO", "token_addr": None, "amount": sign*amount_cro,
        "price_usd": price, "usd_value": usd_value, "realized_pnl": realized,
        "from": frm, "to": to
    })
    link=CRONOS_TX.format(txhash=h)
    send_telegram(
        f"*Native TX* ({'IN' if sign>0 else 'OUT'}) CRO\nHash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount_cro:.6f} CRO\nPrice: ${_fmt_price(price)}\nUSD value: ${_fmt_amount(usd_value)}"
    )


def handle_erc20_tx(t: dict):
    h=t.get("hash") or ""; frm=(t.get("from") or "").lower(); to=(t.get("to") or "").lower()
    if h in _seen_token or settings.WALLET_ADDRESS not in (frm,to): return
    _seen_token.add(h)
    token_addr=(t.get("contractAddress") or "").lower()
    symbol=t.get("tokenSymbol") or (token_addr[:8] if token_addr else "?")
    try: decimals=int(t.get("tokenDecimal") or 18)
    except: decimals=18
    val_raw=t.get("value","0")
    try: amount=int(val_raw)/(10**decimals)
    except: amount=float(val_raw)

    ts=int(t.get("timeStamp") or 0)
    dt=datetime.fromtimestamp(ts, LOCAL_TZ)
    sign= +1 if to==settings.WALLET_ADDRESS else -1

    price=(get_price_usd(token_addr) if token_addr else get_price_usd(symbol)) or 0.0
    usd_value=sign*amount*price

    append_ledger({
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"), "txhash": h or None, "type":"erc20",
        "token": symbol, "token_addr": token_addr or None,
        "amount": sign*amount, "price_usd": price, "usd_value": usd_value,
        "realized_pnl": 0.0, "from": frm, "to": to
    })

    link=CRONOS_TX.format(txhash=h); direction="IN" if sign>0 else "OUT"
    send_telegram(
        f"Token TX ({direction}) {symbol}\nHash: {link}\nTime: {dt.strftime('%H:%M:%S')}\n"
        f"Amount: {sign*amount:.6f} {symbol}\nPrice: ${_fmt_price(price)}\nUSD value: ${_fmt_amount(usd_value)}"
    )

# --- Loops ---

def wallet_monitor_loop():
    send_telegram("📡 Wallet monitor started.")
    while not shutdown_event.is_set():
        try:
            for tx in fetch_latest_wallet_txs(limit=25):
                handle_native_tx(tx)
            for t in fetch_latest_token_txs(limit=100):
                handle_erc20_tx(t)
            replay_today_cost_basis()
        except Exception as e:
            log.exception("wallet monitor error: %s", e)
        for _ in range(settings.WALLET_POLL):
            if shutdown_event.is_set(): break
            time.sleep(1)


def summarize_today_per_asset():
    data=read_json(data_file_for_today(), default={"date": ymd(), "entries": []})
    entries=data.get("entries",[])
    agg={}
    for e in entries:
        sym=(e.get("token") or "?").upper(); addr=(e.get("token_addr") or "").lower()
        key=addr if addr.startswith("0x") else sym
        rec=agg.get(key)
        if not rec:
            rec={"symbol":sym,"token_addr":addr or None,"buy_qty":0.0,"sell_qty":0.0,
                 "net_qty_today":0.0,"net_flow_today":0.0,"realized_today":0.0}
            agg[key]=rec
        amt=float(e.get("amount") or 0.0); usd=float(e.get("usd_value") or 0.0); pr=float(e.get("price_usd") or 0.0)
        rp=float(e.get("realized_pnl") or 0.0)
        if amt>0: rec["buy_qty"]+=amt
        if amt<0: rec["sell_qty"]+=-amt
        rec["net_qty_today"]+=amt
        rec["net_flow_today"]+=usd
        rec["realized_today"]+=rp
    return sorted(agg.values(), key=lambda r: abs(r["net_flow_today"]), reverse=True)


def format_daily_sum_message():
    per=summarize_today_per_asset()
    if not per: return f"🧾 Δεν υπάρχουν σημερινές κινήσεις ({ymd()})."
    tot_real=sum(float(r.get("realized_today",0.0)) for r in per)
    tot_flow=sum(float(r.get("net_flow_today",0.0)) for r in per)
    lines=[f"*🧾 Daily PnL (Today {ymd()}):*"]
    for r in per:
        tok=r.get("symbol") or "?"; flow=float(r.get("net_flow_today",0.0)); real=float(r.get("realized_today",0.0))
        qty=float(r.get("net_qty_today",0.0))
        lines.append(f"• {tok}: realized ${_fmt_amount(real)} | flow ${_fmt_amount(flow)} | qty {_fmt_amount(qty)}")
    lines.append("")
    lines.append(f"*Σύνολο realized σήμερα:* ${_fmt_amount(tot_real)}")
    lines.append(f"*Σύνολο net flow σήμερα:* ${_fmt_amount(tot_flow)}")
    return "\n".join(lines)

# Totals (today|month|all)

def iter_ledger_files_for_scope(scope: str):
    files=[]
    if scope=="today":
        files=[f"transactions_{ymd()}.json"]
    elif scope=="month":
        pref=ymd()[:7]
        try:
            for fn in os.listdir(settings.DATA_DIR):
                if fn.startswith(f"transactions_{pref}") and fn.endswith(".json"): files.append(fn)
        except Exception:
            pass
    else:
        try:
            for fn in os.listdir(settings.DATA_DIR):
                if fn.startswith("transactions_") and fn.endswith(".json"): files.append(fn)
        except Exception:
            pass
    files.sort()
    return [os.path.join(settings.DATA_DIR,fn) for fn in files]

import os

def load_entries_for_totals(scope: str):
    entries=[]
    for path in iter_ledger_files_for_scope(scope):
        data=read_json(path, default=None)
        if not isinstance(data,dict): continue
        for e in data.get("entries",[]):
            sym=(e.get("token") or "?").upper(); amt=float(e.get("amount") or 0.0)
            usd=float(e.get("usd_value") or 0.0); realized=float(e.get("realized_pnl") or 0.0)
            side="IN" if amt>0 else "OUT"
            entries.append({"asset":sym,"side":side,"qty":abs(amt),"usd":usd,"realized_usd":realized})
    return entries


def format_totals(scope: str):
    scope=(scope or "all").lower()
    rows=aggregate_per_asset(load_entries_for_totals(scope))
    if not rows: return f"📊 Totals per Asset — {scope.capitalize()}: (no data)"
    lines=[f"📊 Totals per Asset — {scope.capitalize()}:"]
    for i,r in enumerate(rows,1):
        lines.append(
            f"{i}. {r['asset']}  IN: {_fmt_amount(r['in_qty'])} (${_fmt_amount(r['in_usd'])}) | "
            f"OUT: {_fmt_amount(r['out_qty'])} (${_fmt_amount(r['out_usd'])}) | REAL: ${_fmt_amount(r['realized_usd'])}"
        )
    lines.append(f"\nΣύνολο realized: ${_fmt_amount(sum(float(x['realized_usd']) for x in rows))}")
    return "\n".join(lines)

# Telegram commands (polling)
import requests

def _tg_api(method: str, **params):
    url=f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/{method}"
    try:
        r=requests.get(url, params=params, timeout=30)
        if r.status_code==200: return r.json()
    except Exception:
        pass
    return None


def handle_command(text: str):
    low=text.strip().lower()
    if low.startswith("/status"):
        send_telegram("✅ Running. Wallet monitor, Alerts & Scheduler active.")
    elif low.startswith("/diag"):
        send_telegram(
            "🔧 Diagnostics\n"
            f"WALLET: {settings.WALLET_ADDRESS}\n"
            f"CRONOSRPCURL set: {bool(settings.CRONOS_RPC_URL)}\n"
            f"Etherscan key: {bool(settings.ETHERSCAN_API)}\n"
            f"TZ=Europe/Athens INTRADAYHOURS={settings.INTRADAY_HOURS} EOD={settings.EOD_HOUR:02d}:{settings.EOD_MINUTE:02d}\n"
        )
    elif low.startswith("/rescan"):
        send_telegram("🔄 (Modular v1) Rescan is not implemented in this cut.")
    elif low.startswith("/holdings") or low in ("/show_wallet_assets","/showwalletassets","/show"):
        tot, br, un = compute_holdings_merged()
        if not br:
            send_telegram("📦 Κενά holdings.")
        else:
            lines=["*📦 Holdings (merged):*"]
            for b in br:
                lines.append(f"• {b['token']}: {_fmt_amount(b['amount'])}  @ ${_fmt_price(b.get('price_usd',0))}  = ${_fmt_amount(b.get('usd_value',0))}")
            lines.append(f"\nΣύνολο: ${_fmt_amount(tot)}")
            if abs(un)>EPS: lines.append(f"Unrealized: ${_fmt_amount(un)}")
            send_telegram("\n".join(lines))
    elif low.startswith("/dailysum") or low.startswith("/showdaily"):
        send_telegram(format_daily_sum_message())
    elif low.startswith("/report"):
        # rebuild holdings for report
        tot, br, un = compute_holdings_merged()
        data=read_json(data_file_for_today(), default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
        txt=build_day_report_text(date_str=ymd(), entries=data.get("entries",[]), net_flow=float(data.get("net_usd_flow",0.0)),
                                  realized_today_total=float(data.get("realized_pnl",0.0)), holdings_total=tot, breakdown=br, unrealized=un, data_dir=settings.DATA_DIR)
        send_telegram(txt)
    elif low.startswith("/totals"):
        parts=low.split(); scope=parts[1] if len(parts)>1 and parts[1] in ("today","month","all") else "all"
        send_telegram(format_totals(scope))
    elif low.startswith("/totalstoday"):
        send_telegram(format_totals("today"))
    elif low.startswith("/totalsmonth"):
        send_telegram(format_totals("month"))
    elif low.startswith("/pnl"):
        parts=low.split(); scope=parts[1] if len(parts)>1 and parts[1] in ("today","month","all") else "all"
        send_telegram(format_totals(scope))
    else:
        send_telegram("❓ Commands: /status /diag /rescan /holdings /show /dailysum /report /totals [today|month|all] /totalstoday /totalsmonth /pnl [scope]")


def telegram_long_poll_loop():
    if not settings.TELEGRAM_BOT_TOKEN:
        return
    send_telegram("🤖 Telegram command handler online.")
    offset=None
    while not shutdown_event.is_set():
        try:
            resp=_tg_api("getUpdates", timeout=50, offset=offset, allowed_updates=json.dumps(["message"]))
            if not resp or not resp.get("ok"):
                time.sleep(2); continue
            for upd in resp.get("result",[]):
                offset = upd["update_id"] + 1
                msg=upd.get("message") or {}
                chat_id=str(((msg.get("chat") or {}).get("id") or ""))
                if settings.TELEGRAM_CHAT_ID and str(settings.TELEGRAM_CHAT_ID)!=chat_id:
                    continue
                text=(msg.get("text") or "").strip()
                if not text: continue
                handle_command(text)
        except Exception as e:
            time.sleep(2)

# --- Scheduler (intraday / EOD) ---
_last_intraday_sent=0.0

def scheduler_loop():
    global _last_intraday_sent
    send_telegram("⏱ Scheduler online (intraday/EOD).")
    while not shutdown_event.is_set():
        try:
            now=now_dt()
            if _last_intraday_sent<=0 or (time.time()-_last_intraday_sent)>=settings.INTRADAY_HOURS*3600:
                send_telegram(format_daily_sum_message()); _last_intraday_sent=time.time()
            if now.hour==settings.EOD_HOUR and now.minute==settings.EOD_MINUTE:
                tot, br, un = compute_holdings_merged()
                data=read_json(data_file_for_today(), default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
                txt=build_day_report_text(date_str=ymd(), entries=data.get("entries",[]), net_flow=float(data.get("net_usd_flow",0.0)),
                                          realized_today_total=float(data.get("realized_pnl",0.0)), holdings_total=tot, breakdown=br, unrealized=un, data_dir=settings.DATA_DIR)
                send_telegram(txt)
                time.sleep(65)
        except Exception:
            pass
        for _ in range(20):
            if shutdown_event.is_set(): break
            time.sleep(3)

# --- Entrypoint ---

def _graceful_exit(signum, frame):
    try: send_telegram("🛑 Shutting down.")
    except: pass
    shutdown_event.set()


def main():
    send_telegram("🟢 Starting Cronos DeFi Sentinel (Modular v1).")
    threading.Thread(target=wallet_monitor_loop, name="wallet", daemon=True).start()
    threading.Thread(target=telegram_long_poll_loop, name="telegram", daemon=True).start()
    threading.Thread(target=scheduler_loop, name="scheduler", daemon=True).start()
    while not shutdown_event.is_set():
        time.sleep(1)

if __name__=="__main__":
    signal.signal(signal.SIGINT, _graceful_exit)
    signal.signal(signal.SIGTERM, _graceful_exit)
    main()
