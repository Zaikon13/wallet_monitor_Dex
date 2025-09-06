# reports/ledger.py
# Ledger helpers για cost-basis, realized/unrealized PnL, I/O

import os
import json
from collections import defaultdict
from datetime import datetime

DATA_DIR = "/app/data"


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


def ymd(dt=None):
    if dt is None:
        dt = datetime.now()
    return dt.strftime("%Y-%m-%d")


def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")


def append_ledger(entry: dict):
    """
    Append μια νέα εγγραφή στο σημερινό ledger file και update totals.
    """
    path = data_file_for_today()
    data = read_json(path, default={"date": ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    data["entries"].append(entry)
    data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
    data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0))
    write_json(path, data)


def update_cost_basis(position_qty, position_cost, token_key, signed_amount, price_usd, eps=1e-12):
    """
    Ενημερώνει qty/cost για ένα trade και επιστρέφει realized PnL.
    Συμβατό με τις κλήσεις του main.py (BUY: +amount, SELL: -amount).
    """
    qty = float(position_qty.get(token_key, 0.0))
    cost = float(position_cost.get(token_key, 0.0))
    realized = 0.0

    if signed_amount > eps:  # BUY
        buy_qty = signed_amount
        position_qty[token_key] = qty + buy_qty
        position_cost[token_key] = cost + buy_qty * (price_usd or 0.0)

    elif signed_amount < -eps:  # SELL
        sell_qty_req = -signed_amount
        if qty > eps:
            sell_qty = min(sell_qty_req, qty)
            avg_cost = (cost / qty) if qty > eps else (price_usd or 0.0)
            realized = (price_usd - avg_cost) * sell_qty
            position_qty[token_key] = qty - sell_qty
            position_cost[token_key] = max(0.0, cost - avg_cost * sell_qty)

    return float(realized)


def replay_cost_basis_over_entries(entries, eps=1e-12):
    """
    Παίρνει entries (όπως τα γράφει το ledger) και ξαναχτίζει qty/cost + realized.
    Επιστρέφει (position_qty, position_cost, realized_today_total).
    Ενημερώνει inline το κάθε entry["realized_pnl"].
    """
    position_qty = defaultdict(float)
    position_cost = defaultdict(float)
    realized_total = 0.0

    for e in entries or []:
        token_key = (e.get("token_addr") or (e.get("token") if e.get("token") == "CRO" else None)) or "CRO"
        amt = float(e.get("amount") or 0.0)
        prc = float(e.get("price_usd") or 0.0)
        realized = update_cost_basis(position_qty, position_cost, token_key, amt, prc, eps=eps)
        e["realized_pnl"] = float(realized)
        realized_total += float(realized)

    return position_qty, position_cost, realized_total
