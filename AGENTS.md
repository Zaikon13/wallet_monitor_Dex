# Cronos DeFi Sentinel — Codex / Agents Brief

> Repository: `Zaikon13/wallet_monitor_Dex`  
> Deployment: Railway (TZ: Europe/Athens)

---

## 0) Στόχος
- Κρατάμε canonical `main.py` (~1000–1400 lines).  
- Όλα τα helpers βρίσκονται σε modules (`utils/`, `telegram/`, `reports/`).  
- Ο Codex κάνει μόνο per-file edits, ποτέ ενιαίο mirror.  
- Προτεραιότητα: σταθερότητα, deploy χωρίς errors.

---

## 1) Κανόνες (CRITICAL)
1. **Per-file only**: κάθε edit = πλήρες αρχείο, όχι diff.  
2. **Canonical**: baseline `main.py` στο branch `main` (13/09/2025).  
3. 🚫 Όχι `@diff` patches.  
4. 🚫 Όχι bash/CLI εντολές (ο χρήστης δουλεύει μόνο από GitHub Web UI).  
5. 🚫 Όχι rename σε env vars ή public functions.  
6. ✅ Αν κάτι είναι speculative → βάλε `# TODO:` σχόλιο.  
7. ✅ Όταν παραδίδονται πολλά αρχεία → **Manifest με λίστα αρχείων**.  
8. ✅ Σύντομες εξηγήσεις στα ελληνικά.  

---

## 2) Repo Αρχιτεκτονική
- `main.py` — entrypoint, loops (wallet, dex, alerts, guard, telegram, scheduler).  
- `utils/http.py` — `safe_get`, `safe_json`.  
- `telegram/api.py` — `send_telegram(text)`.  
- `reports/ledger.py` — ledger helpers.  
- `reports/aggregates.py` — aggregations.  
- `reports/day_report.py` — EOD/intraday report text.  
- `telegram/__init__.py`, `reports/__init__.py` — κενά αρχεία.  

**Imports στο main.py:**
```python
from utils.http import safe_get, safe_json
from telegram.api import send_telegram
from reports.day_report import build_day_report_text as _compose_day_report
from reports.ledger import append_ledger, update_cost_basis as ledger_update_cost_basis, replay_cost_basis_over_entries
from reports.aggregates import aggregate_per_asset
