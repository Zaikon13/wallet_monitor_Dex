# MANIFEST — v53 (SPOT split)

**Date:** 2025-10-08 (Europe/Athens)
**Branch:** `work` (tree SHA: _local sandbox_)

## Source of Truth
- Code: GitHub repo **Zaikon13/wallet_monitor_Dex**
- Main SPOT canvas: *Wallet Monitor Dex Spot* (control center)
- Workflows SPOT canvas: *Wallet Monitor Dex — Workflows (SPOT)*

## Scope
Python code, workflows, configs, scripts, tests.
**Canvas keeps only:** `main.py` (canonical ref), `core/config.py`, MANIFEST/INSTRUCTIONS/CHANGELOG.

## Status
**READY** — PR-001 → PR-002 → PR-003 merged, AST-only CI retained.

---

## Present in this Canvas (Refs)
- `main.py` (canonical reference; startup, schedulers, telegram routes)
- `core/config.py` (AppConfig, typed getters, load_config)
- `CHANGELOG_PR_001_002_003.md` (merge summary)
- `PROJECT_INSTRUCTIONS` (rules)

## In Workflows SPOT
- `.github/workflows/*.yml` (full copies live there)

## In Repo (Project → Files) only
- `core/` (alerts, holdings, pricing, providers/, runtime_state, tz, wallet_monitor, watch, signals/)
- `reports/` (aggregates, day_report, ledger, scheduler, weekly)
- `telegram/` (api, dispatcher, commands, formatters, __init__)
- `utils/` (http, __init__)
- `scripts/` (cordex_diag, cordex_ping, snapshot_wallet, smoke*, ast_repair, repo_health, etc.)
- `tests/` (test_smoke.py και λοιπά tests)
- Root: `README.md`, `requirements.txt`, `setup.cfg`, `.gitignore`, `.env` (**REDACTED**), `Procfile`

---

## Changes since v52
- ✅ Integrated holdings refactor (PR-001) with Decimal snapshots, CRO/tCRO merge, and RPC discovery hardening.
- ✅ Wired adapters + runtime surfaces (PR-002) so `main.py`, Telegram, and smoke scripts use a single holdings entrypoint.
- ✅ Added runtime smoke workflow + CLI checks (PR-003) while keeping `.github/workflows/ci.yml` as the AST-only PR gate.
- ✅ Deleted merge-orchestration breadcrumbs from `.github/` after stack landing.

## Pending
- `LICENSE` (MIT)
- Unit tests for `core/config.AppConfig` type coercion (bool/int/float/list & aliases)
- Follow-up docs: scheduler matrix + Telegram command cookbook

## Rules (unchanged)
- Full files only (no diffs/snippets) • PRs via GitHub Web UI
- Canvas = control center (manifest + instructions) • Raw code via Project → Files
- CRO always included; tCRO separate; Unrealized PnL from merged holdings

## Next steps (checklist)
- [ ] Publish PR description summarizing holdings merge train using the updated changelog
- [ ] Monitor runtime-smoke run after merge (expect DRY_RUN=1, no network)
- [ ] Kick off Workflows SPOT sync to ensure AST-only CI file matches repository copy
- [ ] Draft MIT `LICENSE`
