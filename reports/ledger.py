# -*- coding: utf-8 -*-
"""
Ledger helpers
- append_ledger: write daily JSON file
- update_cost_basis: Decimal + FIFO with optional fee_usd
- replay_cost_basis_over_entries: rebuild open positions & realized PnL

Entries use schema:
  {time, txhash, type, token, token_addr, amount, price_usd, usd_value, realized_pnl, from, to}
"""
import os, json
from collections import deque, defaultdict
from decimal import Decimal, getcontext
from datetime import datetime

getcontext().prec = 28

DATA_DIR = "/app/data"

def _day_path(date_str):
    return os.path.join(DATA_DIR, f"transactions_{date_str}.json")


def _today_str():
    return datetime.utcnow().strftime("%Y-%m-%d")


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


def append_ledger(entry: dict):
    d = entry.get("time", "")[:10] or _today_str()
    path = _day_path(d)
    data = read_json(path, default={"date": d, "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    if not isinstance(data, dict):
        data = {"date": d, "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0}
    data.setdefault("entries", []).append(entry)
    try:
        usd = float(entry.get("usd_value") or 0.0)
        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + usd
        rp = float(entry.get("realized_pnl") or 0.0)
        data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + rp
    except Exception:
        pass
    write_json(path, data)


# ---------------- Cost-basis (FIFO, Decimal) ----------------
# position_qty: key -> Decimal qty
# position_cost: key -> Decimal total cost in USD
# lots: key -> deque of (qty:Decimal, price_usd:Decimal)


def update_cost_basis(position_qty, position_cost, token_key, signed_amount, price_usd, fee_usd: float | None = None, eps=Decimal("1e-12")):
    """
    Update positions using FIFO lots; return realized PnL (float) for this trade.
    signed_amount > 0 for buy, < 0 for sell.
    """
    qty = Decimal(str(position_qty.get(token_key, Decimal("0"))))
    cost = Decimal(str(position_cost.get(token_key, Decimal("0"))))
    px  = Decimal(str(price_usd or 0))
    amt = Decimal(str(signed_amount or 0))
    fee = Decimal(str(fee_usd or 0))

    if not hasattr(update_cost_basis, "_lots"):
        update_cost_basis._lots = defaultdict(deque)
    lots = update_cost_basis._lots[token_key]

    realized = Decimal("0")
    if amt > 0:  # buy -> add lot
        lots.append((amt, px))
        qty += amt
        cost += amt * px
    elif amt < 0:  # sell -> consume FIFO
        to_sell = -amt
        while to_sell > eps and lots:
            lot_qty, lot_px = lots[0]
            use = lot_qty if lot_qty <= to_sell else to_sell
            realized += (px - lot_px) * use
            lot_qty -= use
            to_sell -= use
            qty -= use
            cost -= lot_px * use
            if lot_qty <= eps:
                lots.popleft()
            else:
                lots[0] = (lot_qty, lot_px)
        # fees reduce realized PnL
        realized -= fee

    position_qty[token_key] = qty
    position_cost[token_key] = cost if qty > eps else Decimal("0")
    return float(realized)


def replay_cost_basis_over_entries(position_qty, position_cost, entries, eps=Decimal("1e-12")):
    """Rebuild positions and compute total realized from the given entries."""
    position_qty.clear(); position_cost.clear()
    if hasattr(update_cost_basis, "_lots"):
        update_cost_basis._lots.clear()
    total_realized = Decimal("0")

    for e in entries or []:
        key = (e.get("token_addr") or e.get("token") or "?")
        amt = float(e.get("amount") or 0.0)
        px  = float(e.get("price_usd") or 0.0)
        fee = float(e.get("fee_usd") or 0.0)
        total_realized += Decimal(str(update_cost_basis(position_qty, position_cost, key, amt, px, fee_usd=fee, eps=eps)))

    return float(total_realized)


__all__ = [
    "append_ledger",
    "update_cost_basis",
    "replay_cost_basis_over_entries",
]
