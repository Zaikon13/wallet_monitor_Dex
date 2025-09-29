# reports/ledger.py
# Safe, drop-in συμβατό με το current main.py
# Παρέχει:
#   - append_ledger(entry: dict) -> None
#   - update_cost_basis(qty_map: dict, cost_map: dict, key: str, signed_amount: float, price: float, eps: float=1e-12) -> float
#   - replay_cost_basis_over_entries(qty_map: dict, cost_map: dict, entries: Iterable[dict], eps: float=1e-12) -> float
#
# Σχεδιασμένο ώστε να ΜΗ ρίχνει το process: όλα τα I/O γίνονται με try/except, με ασφαλή defaults.

from __future__ import annotations

import os
import json
import time
import logging
from typing import Dict, Any, Iterable
from datetime import datetime

log = logging.getLogger("ledger")

# Προσπαθούμε να έχουμε Telegram alert, αλλά δεν το απαιτούμε (no hard dependency)
def _try_send_telegram(msg: str) -> None:
    try:
        from telegram.api import send_telegram
        try:
            send_telegram(msg)
        except Exception as e:
            log.debug("telegram send failed: %s", e)
    except Exception:
        pass

DATA_DIR = "/app/data"

def _ensure_data_dir() -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as e:
        # δεν ρίχνουμε το app — απλά log
        log.error("Failed to create DATA_DIR %s: %s", DATA_DIR, e)

def _ymd(ts: float | None = None) -> str:
    try:
        return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d")
    except Exception:
        # safe fallback
        return time.strftime("%Y-%m-%d")

def _today_file() -> str:
    return os.path.join(DATA_DIR, f"transactions_{_ymd()}.json")

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
        _try_send_telegram(f"⚠️ Failed to write ledger file:\n{path}\n{e}")

def append_ledger(entry: Dict[str, Any]) -> None:
    """
    Δέχεται entry στο format που παράγει το main.py (type 'native' ή 'erc20').
    Γράφει στο /app/data/transactions_YYYY-MM-DD.json:
    {
      "date": "YYYY-MM-DD",
      "entries": [...],
      "net_usd_flow": float,
      "realized_pnl": float
    }
    """
    try:
        if not isinstance(entry, dict):
            return
        _ensure_data_dir()
        path = _today_file()
        date_str = _ymd()

        data = _read_json(path, default={"date": date_str, "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})

        # totals
        try:
            usd = float(entry.get("usd_value") or 0.0)
        except Exception:
            usd = 0.0
        try:
            rp = float(entry.get("realized_pnl") or 0.0)
        except Exception:
            rp = 0.0

        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + usd
        data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + rp

        # append + sort
        data_entries = data.get("entries")
        if not isinstance(data_entries, list):
            data_entries = []
        data_entries.append(entry)
        try:
            data_entries.sort(key=lambda e: e.get("time") or "")
        except Exception:
            pass
        data["entries"] = data_entries

        _write_json(path, data)
    except Exception as e:
        log.error("append_ledger failed: %s", e)
        _try_send_telegram(f"⚠️ append_ledger error: {e}")

def update_cost_basis(
    qty_map: Dict[str, float],
    cost_map: Dict[str, float],
    key: str,
    signed_amount: float,
    price: float | None,
    eps: float = 1e-12
) -> float:
    """
    FIFO με aggregate-lot (μέσο κόστος) — ακριβώς όπως το περιμένει το main.py.
    Επιστρέφει realized PnL για ΜΙΑ κίνηση.
    """
    try:
        if not key:
            return 0.0
        amt = float(signed_amount or 0.0)
        px = float(price or 0.0)
    except Exception:
        return 0.0

    q = float(qty_map.get(key, 0.0) or 0.0)
    c = float(cost_map.get(key, 0.0) or 0.0)

    realized = 0.0
    try:
        if amt > eps:
            # BUY
            qty_map[key] = q + amt
            cost_map[key] = c + amt * px
        elif amt < -eps:
            # SELL
            sell_qty = min(-amt, q) if q > eps else 0.0
            if sell_qty > eps:
                avg_cost = (c / q) if q > eps else px
                proceeds = sell_qty * px
                cost_out = sell_qty * avg_cost
                realized = proceeds - cost_out
                new_q = q - sell_qty
                new_c = max(0.0, c - cost_out)
                qty_map[key] = new_q
                cost_map[key] = new_c
        # else: zero move => realized stays 0
    except Exception as e:
        log.debug("update_cost_basis math error: %s", e)

    # καθάρισμα αριθμητικού θορύβου
    try:
        if abs(qty_map.get(key, 0.0) or 0.0) < eps:
            qty_map[key] = 0.0
        if abs(cost_map.get(key, 0.0) or 0.0) < eps:
            cost_map[key] = 0.0
    except Exception:
        pass

    return float(realized)

def replay_cost_basis_over_entries(
    qty_map: Dict[str, float],
    cost_map: Dict[str, float],
    entries: Iterable[Dict[str, Any]] | None,
    eps: float = 1e-12
) -> float:
    """
    Επανα-υπολογίζει realized PnL πάνω στις εγγραφές της ημέρας (in-place θέση στο πεδίο "realized_pnl").
    Επιστρέφει το συνολικό realized PnL.
    Περιμένει κάθε entry να έχει:
        - "amount" (signed +/−),
        - "price_usd" (float),
        - "token_addr" (0x..) ή "token" (SYM) για προσδιορισμό key.
    """
    total = 0.0
    if not entries:
        return 0.0

    for e in entries:
        try:
            amt = float(e.get("amount") or 0.0)
            px = float(e.get("price_usd") or 0.0)
            addr = (e.get("token_addr") or "").strip().lower()
            sym = (e.get("token") or "").strip()
            key = addr if (addr.startswith("0x") and len(addr) == 42) else (sym.upper() if sym else None)
            if not key:
                e["realized_pnl"] = float(e.get("realized_pnl") or 0.0)
                continue
            rp = update_cost_basis(qty_map, cost_map, key, amt, px, eps=eps)
            e["realized_pnl"] = float(rp)
            total += float(rp)
        except Exception as ex:
            # Δεν σταματάμε το replay για μια χαλασμένη εγγραφή.
            log.debug("replay_cost_basis_over_entries skipped entry due to error: %s", ex)
            try:
                e["realized_pnl"] = float(e.get("realized_pnl") or 0.0)
            except Exception:
                pass
    return float(total)
