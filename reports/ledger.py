# reports/ledger.py
import os, json
from core.tz import ymd

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
os.makedirs(DATA_DIR, exist_ok=True)
EPSILON=1e-12

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

def append_ledger(entry: dict):
    path=data_file_for_today()
    data=read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    data["entries"].append(entry)
    data["net_usd_flow"]=float(data.get("net_usd_flow",0.0))+float(entry.get("usd_value") or 0.0)
    data["realized_pnl"]=float(data.get("realized_pnl",0.0))+float(entry.get("realized_pnl") or 0.0)
    write_json(path, data)

def replay_cost_basis_over_entries(pos_qty: dict, pos_cost: dict, entries: list, eps: float = EPSILON) -> float:
    total_realized=0.0
    for e in entries or []:
        key=(e.get("token_addr") or "").lower() or (e.get("token") or "").upper()
        amt=float(e.get("amount") or 0.0)
        pr=float(e.get("price_usd") or 0.0)
        total_realized += update_cost_basis(pos_qty,pos_cost,key,amt,pr,eps)
    return total_realized

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
