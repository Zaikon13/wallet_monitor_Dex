# reports/ledger.py
# Drop-in adapter για το current main.py
# - Γράφει/διαβάζει /app/data/transactions_YYYY-MM-DD.json
# - Παρέχει update_cost_basis() & replay_cost_basis_over_entries() με τις υπογραφές που περιμένει το main.py

from __future__ import annotations
import os, json, time
from typing import Dict, Any, Iterable, List, Tuple
from datetime import datetime

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)

def _ymd(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d")

def data_file_for_today() -> str:
    return os.path.join(DATA_DIR, f"transactions_{_ymd()}.json")

def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def append_ledger(entry: Dict[str, Any]) -> None:
    """
    Δέχεται entry στο format που γράφει το main.py:
      {
        "time":"YYYY-MM-DD HH:MM:SS", "txhash":..., "type":"native|erc20",
        "token":"CRO|SYM", "token_addr": "0x.."|None,
        "amount": +/-, "price_usd": float, "usd_value": float,
        "realized_pnl": float, "from": "...", "to": "..."
      }
    και το προσθέτει στο /app/data/transactions_YYYY-MM-DD.json,
    ενημερώνοντας net_usd_flow & realized_pnl.
    """
    if not isinstance(entry, dict):
        return
    path = data_file_for_today()
    date_str = _ymd()
    data = _read_json(path, default={"date": date_str, "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})

    # ενημερώσεις totals
    usd = float(entry.get("usd_value") or 0.0)
    rp  = float(entry.get("realized_pnl") or 0.0)
    data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + usd
    data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + rp

    # append + sort by time (αν υπάρχει)
    data["entries"].append(entry)
    try:
        data["entries"].sort(key=lambda e: e.get("time") or "")
    except Exception:
        pass

    _write_json(path, data)

def update_cost_basis(
    qty_map: Dict[str, float],
    cost_map: Dict[str, float],
    key: str,
    signed_amount: float,
    price: float | None,
    eps: float = 1e-12
) -> float:
    """
    FIFO σε in-memory δομές (qty_map/cost_map) και επιστροφή realized PnL για μία κίνηση.
      - key: "CRO" ή συμβόλαιο 0x.. ή σύμβολο
      - signed_amount: +buy, -sell (σε τεμάχια)
      - price: τιμή USD ανά τεμάχιο
    """
    if not key:
        return 0.0
    try:
        amt = float(signed_amount or 0.0)
        px  = float(price or 0.0)
    except Exception:
        return 0.0

    q = float(qty_map.get(key, 0.0) or 0.0)
    c = float(cost_map.get(key, 0.0) or 0.0)

    realized = 0.0
    if amt > eps:
        # BUY: αυξάνουμε ποσότητα & συνολικό κόστος
        qty_map[key]  = q + amt
        cost_map[key] = c + amt * px
    elif amt < -eps:
        # SELL: ρευστοποίηση FIFO με μέσο κόστος (κρατάμε μόνο aggregate lot)
        sell_qty = min(-amt, q) if q > eps else 0.0
        if sell_qty > eps:
            avg_cost = (c / q) if q > eps else px
            proceeds = sell_qty * px
            cost_out = sell_qty * avg_cost
            realized = proceeds - cost_out
            new_q = q - sell_qty
            new_c = max(0.0, c - cost_out)
            qty_map[key]  = new_q
            cost_map[key] = new_c
        # αν πουλήσαμε παραπάνω από το open (δεν πρέπει), αγνοούμε το extra
    else:
        # μηδενική κίνηση
        realized = 0.0

    # καθάρισμα μικρών σφαλμάτων
    if abs(qty_map.get(key, 0.0) or 0.0) < eps:
        qty_map[key] = 0.0
    if abs(cost_map.get(key, 0.0) or 0.0) < eps:
        cost_map[key] = 0.0

    return float(realized)

def replay_cost_basis_over_entries(
    qty_map: Dict[str, float],
    cost_map: Dict[str, float],
    entries: Iterable[Dict[str, Any]] | None,
    eps: float = 1e-12
) -> float:
    """
    Εφαρμόζει σειριακά την update_cost_basis() πάνω σε entries (του ημερήσιου αρχείου)
    και επιστρέφει το συνολικό realized PnL.
    Γράφει επίσης το realized_pnl μέσα σε κάθε entry in-place (αν υπάρχει πεδίο).
    Περιμένει entries με πεδία:
      - "amount" (signed), "price_usd" (float), και προαιρετικά "realized_pnl"
      - key = token_addr (0x..) αν υπάρχει, αλλιώς token (SYM)
    """
    total_realized = 0.0
    if not entries:
        return 0.0

    for e in entries:
        try:
            amt = float(e.get("amount") or 0.0)
            px  = float(e.get("price_usd") or 0.0)
            addr = (e.get("token_addr") or "").lower().strip()
            sym  = (e.get("token") or "").strip()
            key  = addr if (addr.startswith("0x") and len(addr) == 42) else (sym.upper() if sym else None)
            if not key:
                e["realized_pnl"] = 0.0
                continue
            realized = update_cost_basis(qty_map, cost_map, key, amt, px, eps=eps)
            e["realized_pnl"] = float(realized)
            total_realized += float(realized)
        except Exception:
            # σε περίπτωση περίεργης εγγραφής, δεν σταματάμε το replay
            e["realized_pnl"] = float(e.get("realized_pnl") or 0.0)

    return float(total_realized)
