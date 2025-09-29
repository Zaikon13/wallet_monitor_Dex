# reports/ledger.py
# Drop-in, fully compatible με το current main.py + day_report.py
# Παρέχει:
#   append_ledger(entry: dict) -> None
#   update_cost_basis(qty_map: dict, cost_map: dict, key: str, signed_amount: float, price: float|None, eps: float=1e-12) -> float
#   replay_cost_basis_over_entries(qty_map: dict, cost_map: dict, entries: Iterable[dict], eps: float=1e-12) -> float
#   read_ledger(day: str) -> list[dict]
#   list_days() -> list[str]
#   data_file_for_today() -> str
#   iter_all_entries() -> Iterator[dict]

from __future__ import annotations
import os
import json
import time
import logging
from typing import Dict, Any, Iterable, Iterator, List
from datetime import datetime

log = logging.getLogger("ledger")

# ———————————————————————————————————————
# Helpers
# ———————————————————————————————————————
DATA_DIR = "/app/data"

def _ensure_data_dir() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as e:
        log.error("Failed to create DATA_DIR %s: %s", DATA_DIR, e)

def _ymd(ts: float | None = None) -> str:
    try:
        return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d")
    except Exception:
        return time.strftime("%Y-%m-%d")

def _today_file() -> str:
    return os.path.join(DATA_DIR, f"transactions_{_ymd()}.json")

def data_file_for_today() -> str:
    return _today_file()

def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path: str, obj: Any) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.error("Failed to write ledger file %s: %s", path, e)

def _is_day_filename(fn: str) -> bool:
    if not fn.startswith("transactions_") or not fn.endswith(".json"):
        return False
    day = fn[len("transactions_"):-len(".json")]
    try:
        datetime.strptime(day, "%Y-%m-%d")
        return True
    except Exception:
        return False

# ———————————————————————————————————————
# Write API used by main.py
# ———————————————————————————————————————
def append_ledger(entry: Dict[str, Any]) -> None:
    """
    Εγγραφή μίας κίνησης στο αρχείο ημέρας:
      /app/data/transactions_YYYY-MM-DD.json
    Το σχήμα του αρχείου:
      {"date": "YYYY-MM-DD", "entries": [...], "net_usd_flow": float, "realized_pnl": float}
    """
    try:
        if not isinstance(entry, dict):
            return
        _ensure_data_dir()
        path = _today_file()
        date_str = _ymd()

        data = _read_json(path, default={"date": date_str, "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})

        usd = 0.0
        rp  = 0.0
        try: usd = float(entry.get("usd_value") or 0.0)
        except Exception: pass
        try: rp  = float(entry.get("realized_pnl") or 0.0)
        except Exception: pass

        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + usd
        data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + rp

        lst = data.get("entries")
        if not isinstance(lst, list):
            lst = []
        lst.append(entry)
        try:
            lst.sort(key=lambda e: e.get("time") or "")
        except Exception:
            pass
        data["entries"] = lst

        _write_json(path, data)
    except Exception as e:
        log.error("append_ledger failed: %s", e)

def update_cost_basis(
    qty_map: Dict[str, float],
    cost_map: Dict[str, float],
    key: str,
    signed_amount: float,
    price: float | None,
    eps: float = 1e-12
) -> float:
    """
    Απλοποιημένο FIFO (aggregate-lot) που περιμένει το main.py.
    - BUY (+amt): αυξάνει qty & cost.
    - SELL (-amt): ρευστοποιεί μέχρι το διαθέσιμο qty με avg cost.
    Επιστρέφει realized PnL της κίνησης.
    """
    try:
        if not key:
            return 0.0
        amt = float(signed_amount or 0.0)
        px  = float(price or 0.0)
    except Exception:
        return 0.0

    q = float(qty_map.get(key, 0.0) or 0.0)
    c = float(cost_map.get(key, 0.0) or 0.0)
    realized = 0.0

    if amt > eps:
        # BUY
        qty_map[key]  = q + amt
        cost_map[key] = c + amt * px
    elif amt < -eps:
        # SELL
        sell_qty = min(-amt, q) if q > eps else 0.0
        if sell_qty > eps:
            avg_cost = (c / q) if q > eps else px
            proceeds = sell_qty * px
            cost_out = sell_qty * avg_cost
            realized = proceeds - cost_out
            qty_map[key]  = q - sell_qty
            cost_map[key] = max(0.0, c - cost_out)

    # καθάρισμα θορύβου
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
    Recompute realized PnL για μια λίστα εγγραφών (in-place στο πεδίο 'realized_pnl').
    Περιμένει: 'amount', 'price_usd' και key από 'token_addr' (0x..) ή 'token' (SYM).
    """
    total = 0.0
    if not entries:
        return 0.0

    for e in entries:
        try:
            amt = float(e.get("amount") or 0.0)
            px  = float(e.get("price_usd") or 0.0)
            addr = (e.get("token_addr") or "").strip().lower()
            sym  = (e.get("token") or "").strip()
            key  = addr if (addr.startswith("0x") and len(addr) == 42) else (sym.upper() if sym else None)
            if not key:
                # αν δεν υπάρχει key, αφήνουμε ό,τι έχει
                e["realized_pnl"] = float(e.get("realized_pnl") or 0.0)
                continue
            rp = update_cost_basis(qty_map, cost_map, key, amt, px, eps=eps)
            e["realized_pnl"] = float(rp)
            total += float(rp)
        except Exception as ex:
            log.debug("replay skip due to error: %s", ex)
            try:
                e["realized_pnl"] = float(e.get("realized_pnl") or 0.0)
            except Exception:
                pass
    return float(total)

# ———————————————————————————————————————
# Read API needed by day_report.py
# ———————————————————————————————————————
def _day_path(day: str) -> str:
    return os.path.join(DATA_DIR, f"transactions_{day}.json")

def _is_valid_day(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except Exception:
        return False

def list_days() -> List[str]:
    _ensure_data_dir()
    try:
        names = []
        for fn in os.listdir(DATA_DIR):
            if _is_day_filename(fn):
                day = fn[len("transactions_"):-len(".json")]
                if _is_valid_day(day):
                    names.append(day)
        names.sort()
        return names
    except Exception:
        return []

def read_ledger(day: str) -> List[Dict[str, Any]]:
    """
    Επιστρέφει τη λίστα εγγραφών για τη συγκεκριμένη ημέρα.
    Αν το αρχείο δεν υπάρχει/είναι άκυρο, γυρνάει [].
    """
    try:
        path = _day_path(day)
        data = _read_json(path, default=None)
        if not isinstance(data, dict):
            return []
        entries = data.get("entries") or []
        if not isinstance(entries, list):
            return []
        try:
            entries.sort(key=lambda e: e.get("time") or "")
        except Exception:
            pass
        return entries
    except Exception as e:
        log.debug("read_ledger(%s) error: %s", day, e)
        return []

def iter_all_entries() -> Iterator[Dict[str, Any]]:
    for d in list_days():
        for e in read_ledger(d):
            yield e
