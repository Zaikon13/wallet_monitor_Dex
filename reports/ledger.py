# reports/ledger.py
import os
import json
from datetime import datetime

DATA_DIR = "/app/data"
os.makedirs(DATA_DIR, exist_ok=True)

def _ymd():
    return datetime.now().strftime("%Y-%m-%d")

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

def data_file_for_today():
    return os.path.join(DATA_DIR, f"transactions_{_ymd()}.json")

def append_ledger(entry: dict):
    path = data_file_for_today()
    data = read_json(path, default={"date": _ymd(), "entries": [], "net_usd_flow": 0.0, "realized_pnl": 0.0})
    data["entries"].append(entry)
    try:
        data["net_usd_flow"] = float(data.get("net_usd_flow", 0.0)) + float(entry.get("usd_value", 0.0) or 0.0)
    except Exception:
        pass
    try:
        data["realized_pnl"] = float(data.get("realized_pnl", 0.0)) + float(entry.get("realized_pnl", 0.0) or 0.0)
    except Exception:
        pass
    write_json(path, data)
