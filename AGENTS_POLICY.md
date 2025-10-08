# AGENTS — Canonical Policies & Health Criteria

## Goals (standing)
- Keep PR checks green with AST-only & safe smoke.
- Protect `main.py` from breakages; runtime smoke with DRY_RUN + timeout.
- Report actionable issues and propose minimal PR plans.

## Repo Health Criteria
1) **Imports/Modules**
   - All top-level packages importable without side-effects.
   - Mandatory modules present: `core/`, `utils/`, `telegram/`, `reports/`.
   - `core/pricing.py` exposes bounded caches and price getters.
   - `core/holdings.py` reconciles CRO vs tCRO; no double counting; FIFO.

2) **Scheduler/Runtime**
   - `main.py` respects `DRY_RUN=1` (no schedulers/network).
   - EOD/weekly tasks bound to config (`EOD_TIME`, `WEEKLY_DOW`).

3) **Telegram**
   - `telegram/api.py` provides `send_telegram_message(text: str)`.
   - Safe Markdown escaping & chunking; long-poll backoff present.

4) **Pricing**
   - Dexscreener primary; history fallback; WCRO canonical path to USDT.
   - Bounded caches (max size + TTL) to avoid API overload.

5) **PnL/Reports**
   - FIFO (Decimal) cost basis; daily/weekly reports build without network.
   - Intraday/EOD checkpoints exist.

6) **Config**
   - `core/config.AppConfig` loads env without side-effects on import.
   - `WEEKLY_DOW` accepts `0..6` or `Mon..Sun`.

## Exit Behavior
- Health Check defaults to **non-failing** (advisory).
- Set `FAIL_ON=1` to fail CI on CRITICAL issues.
