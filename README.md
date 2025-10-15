# Cronos DeFi Sentinel

[![CI (AST only)](https://github.com/Zaikon13/wallet_monitor_Dex/actions/workflows/ci.yml/badge.svg)](https://github.com/Zaikon13/wallet_monitor_Dex/actions/workflows/ci.yml)
[![Tests (manual)](https://github.com/Zaikon13/wallet_monitor_Dex/actions/workflows/tests-manual.yml/badge.svg)](https://github.com/Zaikon13/wallet_monitor_Dex/actions/workflows/tests-manual.yml)
[![Runtime Smoke](https://github.com/Zaikon13/wallet_monitor_Dex/actions/workflows/runtime-smoke.yml/badge.svg)](https://github.com/Zaikon13/wallet_monitor_Dex/actions/workflows/runtime-smoke.yml)

Compact Python bot (<1000 lines) που παρακολουθεί το Cronos wallet σου,
κρατάει ledger με συναλλαγές, υπολογίζει PnL (realized & unrealized),
και στέλνει reports / alerts στο Telegram.

---

## 🚀 Features

- Wallet monitoring (CRO + ERC20 στο Cronos)
- Dexscreener integration για τιμές/μεταβολές
- Ledger με cost-basis (FIFO/avg) και reports
- Intraday & End-of-Day αυτόματα reports
- Telegram bot με εντολές:
  - `/status`, `/holdings`, `/show`
  - `/dailysum`, `/totals`, `/totalsmonth`, `/totalstoday`
  - `/report`, `/rescan`

---

## 📦 Requirements

- Python 3.12+
- Dependencies στο `requirements.txt`:
# probe-codex-1
