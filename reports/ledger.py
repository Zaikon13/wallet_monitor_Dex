# reports/ledger.py (WITH I/O)
from __future__ import annotations
import os
import json
from datetime import datetime

# -----------------------
# Generic JSON helpers
# -----------------------
def read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def write_json(path: str, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

# -----------------------
# Filenames / paths
# -----------------------
def ymd_today() -> str:
    # απλό, χωρίς εξάρτηση από το main – το TZ το έχει ρυθμίσει ήδη το process
    return datetime.now().strftime("%Y-%m-%d")

def data_file_for_today(data_dir: str) -> str:
    return os.path.join(data_dir, f"transactions_{ymd_today()}.json")

# -----------------------
# Ledger append (pure I/O)
# -----------------------
def append_ledger(entry: dict, data_dir: str):
    """
    Προσθέτει ένα entry στο σημερινό αρχείο και ενημερώνει
    τα aggregate πεδία net_usd_flow & realized_pnl.
    """
    path = data_file_for_today(data_dir)
    data = read_json(path, default={"date": ymd_today(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    data["entries"].append(entry)
    data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0))
    data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0))
    write_json(path, data)
