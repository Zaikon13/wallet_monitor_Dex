# MANIFEST — v53 (SPOT split)

**Date:** 2025-09-30 (Europe/Athens)  
**Branch:** `main` (tree SHA: `3e325d51ad319af0a8d85fe25ffdbdc755aa8597`)

## Source of Truth
- Code: GitHub repo **Zaikon13/wallet_monitor_Dex**
- Main SPOT canvas: *Wallet Monitor Dex Spot* (control center)
- Workflows SPOT canvas: *Wallet Monitor Dex — Workflows (SPOT)*

## Scope
Python code, workflows, configs, scripts, tests.  
**Canvas keeps only:** `main.py` (canonical ref), `core/config.py`, MANIFEST/INSTRUCTIONS/CHANGELOG.

## Status
**PARTIAL** → becomes **READY** when v53 PR merges.

---

## Present in this Canvas (Refs)
- `main.py` (canonical reference; startup, schedulers, telegram routes)
- `core/config.py` (AppConfig, typed getters, load_config)
- `PROJECT_INSTRUCTIONS` (rules)
- `CHANGELOG / TODO`

## In Workflows SPOT
- `.github/workflows/*.yml` (all workflow files, one section per file)

## In Repo (Project → Files) only
- `core/` (alerts, holdings, pricing, providers/, runtime_state, tz, wallet_monitor, watch, signals/)
- `reports/` (aggregates, day_report, ledger, scheduler, weekly)
- `telegram/` (api, dispatcher, commands, formatters, __init__)
- `utils/` (http, __init__)
- `scripts/` (cordex_diag, cordex_ping, snapshot_wallet)
- `tests/` (test_smoke.py και λοιπά tests)
- Root: `README.md`, `requirements.txt`, `setup.cfg`, `.gitignore`, `.env` (**REDACTED**), `Procfile`

---

## Changes since v52
- **Moved:** όλα τα workflow YAMLs σε **Workflows SPOT** (εδώ κρατάμε μόνο pointers).
- **Kept:** Η Canvas κρατά **μόνο** `main.py` ref + `core/config.py` + MANIFEST + RULES + CHANGELOG.
- **Updated:** `PROJECT_INSTRUCTIONS` / rules.
- **No code edits** στα modules· μόνο οργανωτικές αλλαγές.

## Pending
- `LICENSE` (MIT)
- Μικρά unit tests για `core/config.load_config()` (bool/int/float/list & aliases)

## Rules (unchanged)
- Full files only (no diffs/snippets) • PRs via GitHub Web UI  
- Canvas = control center (manifest + instructions) • Raw code via Project → Files  
- CRO always included; tCRO separate; Unrealized PnL from merged holdings

## Next steps (checklist)
- [ ] Verify Workflows SPOT contains all `.yml`
- [ ] Ensure main SPOT shows only: `main.py` ref, `core/config.py`, MANIFEST, INSTRUCTIONS, CHANGELOG/TODO
- [ ] Open PR `manifest-v53 → main` (description: “SPOT split; canvas slim; workflows isolated”)
- [ ] After merge → set v53 **READY** and start v54 for next edits

---

## File Inventory — sizes & blob SHAs (audit @ `main`)

