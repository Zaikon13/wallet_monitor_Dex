import os, json
from pathlib import Path
BASE=Path("./.ledger"); BASE.mkdir(exist_ok=True, parents=True)
def _p(day): return BASE/f"{day}.json"
def append_ledger(day, entry):
    p=_p(day); data=[]
    if p.exists():
        try: data=json.loads(p.read_text(encoding="utf-8"))
        except Exception: data=[]
    data.append(entry)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
def read_ledger(day):
    p=_p(day)
    if not p.exists(): return []
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return []
