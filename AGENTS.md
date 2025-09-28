# AGENTS.md (για copy-paste στο repo)

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
````

## 3) Environment (Railway)

**Χωρίς secrets**. Canonical names:

* `TELEGRAM_BOT_TOKEN`
* `TELEGRAM_CHAT_ID=5307877340`
* `WALLET_ADDRESS=0xEa53D79ce2A915033e6b4C5ebE82bb6b292E35Cc`
* `ETHERSCAN_API`
* `CRONOS_RPC_URL=https://cronos-evm-rpc.publicnode.com`
* Thresholds/Intervals: `ALERTS_INTERVAL_MIN`, `DEX_POLL`, `DISCOVER_*`, `EOD_HOUR`, `EOD_MINUTE`, `TZ=Europe/Athens`

---

## 4) Requirements

```text
requests==2.32.3
web3==6.20.1
setuptools==69.5.1
tzdata==2024.1
python-dotenv==1.0.1
# schedule==1.2.1  # μόνο αν ξαναχρησιμοποιηθεί
```

---

## 5) Telegram API

* Function: **`send_telegram(text)`** μόνο.
* Base URL: `https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}`
* `parse_mode="Markdown"`. Αν error → αφαιρείται ή γίνεται escape.
* Long messages split σε chunks ~3800 chars.

---

## 6) Checklist πριν commit

* [ ] Κάθε f-string κλείνει σωστά.
* [ ] Imports όπως §2.
* [ ] Υπάρχουν `reports/__init__.py` & `telegram/__init__.py`.
* [ ] Στο boot: `send_telegram("🟢 Starting Cronos DeFi Sentinel.")`.
* [ ] Στο exit: `send_telegram("🛑 Shutting down.")`.
* [ ] Όλες οι commands (`/status`, `/holdings`, `/report` κ.λπ.) καλούν `send_telegram`.

---

## 7) Manifest Template

Όταν παραδίδονται πολλά αρχεία, χρησιμοποίησε το εξής format:


## Manifest (4 αρχεία)

1. utils/http.py
2. telegram/api.py
3. reports/ledger.py
4. reports/day_report.py

---

### utils/http.py
```python
# full content …
```

### telegram/api.py
```python
# full content …
```

### reports/ledger.py
```python
# full content …
```

### reports/day_report.py
```python
# full content …
```

---

## 8) Running pip (Railway)

Για να επιβεβαιώσεις ότι το image έχει τις σωστές βιβλιοθήκες, στο build log θα τρέξει αυτόματα:

```bash
pip install -r requirements.txt
```

Αν θες να το κάνεις χειροκίνητα σε dev περιβάλλον:

```bash
pip install requests==2.32.3 web3==6.20.1 setuptools==69.5.1 tzdata==2024.1 python-dotenv==1.0.1
```

---

*This AGENTS.md είναι η πηγή αλήθειας για το Codex στο project.*

---

Θέλεις να το ανεβάσουμε στο repo σου ως **`AGENTS.md`** στο root, ώστε να το βλέπει και ο Codex;
```
