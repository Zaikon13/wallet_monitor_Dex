## Purpose

Short, actionable instructions for an AI coding agent to be immediately productive in this repository.
Read these first: they encode project-specific conventions and the minimal context an edit must preserve.

## Files to read immediately
- `AGENTS.md` — repo-level agent rules and delivery constraints (full-file mode).
- `MANIFEST.md`, `CHECKS.md`, `SUMMARY.md` — build/CI expectations and pinned environment vars.
- `main.py` — canonical startup, scheduling, and global runtime state (lots of globals; read carefully).
- `core/config.py` — centralized typed getters and `AppConfig` used in tests and refactors.
- `reports/ledger.py` — authoritative cost-basis logic (Decimal-based, FIFO modern path + legacy compat).
- `telegram/api.py` — Telegram messaging helpers (chunking + Markdown fallback).

## High-level architecture (what to keep in mind)
- main.py is the runtime orchestrator: it wires scheduling, RPC calls, dexscreener pricing, ledger append, and Telegram alerts.
- core/ contains business logic helpers (pricing, wallet monitor, guards, runtime state). Prefer small edits here.
- reports/ holds persistence + accounting (`.ledger` by default) and is the single source of truth for cost-basis math.
- telegram/ contains the send/format/dispatch surface; use `send_telegram` (safe chunking + fallback) for notifications.
- scripts/ are operational helpers (`cordex_diag.py`, `snapshot_wallet.py`) used by automation; review them before changing CI behavior.
- Data locations: `DATA_DIR` (default `/app/data`) and `LEDGER_DIR` (default `./.ledger`). Use the existing helpers (`data_file_for_day`, `data_file_for_today`).

## Project-specific conventions & patterns
- Full-file edits: AGENTS.md requires delivering full, deploy-ready files rather than diffs/snippets.
- Dual-mode APIs: `reports/ledger.py` exposes dual signatures (modern FIFO vs legacy avg-cost). Preserve compatibility when refactoring.
- Money math: use `Decimal` in `reports/ledger.py`; ledger JSON serializes Decimal as strings. Avoid reintroducing float-only paths in ledger code.
- JSON ledger shape: entries include keys like `time`, `asset`, `side`, `qty`, `price_usd`, `usd`, `fee_usd`, `realized_usd` (see `append_ledger`).
- Telegram: prefer `telegram.api.send_telegram(text)` — it splits long messages and falls back to plain text when Markdown fails.
- Config: prefer `core.config.load_config()` and `AppConfig` getters rather than ad-hoc `os.getenv` in new code.
- Timezone: the app uses `TZ` / `Europe/Athens` by default; tests and datetime helpers live in `core/tz.py`.

## Integration points & external dependencies
- RPC: `web3` is used for Cronos RPC (`CRONOS_RPC_URL`). Guard for missing RPC (main.py may operate in degraded mode).
- Pricing: dexscreener endpoints are relied on in `get_price_usd` logic inside `main.py` (pick best liquidity pair). Respect caching (`PRICE_CACHE`) if modifying.
- Etherscan-like API: `ETHERSCAN_API` is used via `fetch_latest_*_txs` routes.
- Telegram: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (do not print secrets in logs).

## Tests, CI, and quick commands
- Tests are pytest-based and mostly import modules (smoke-style). Run locally to catch import-time regressions.

PowerShell example (recommended):
```powershell
python -m pip install -r requirements.txt
pytest -q
```

Run the app locally (degraded if you don't provide secrets):
```powershell
setx TELEGRAM_BOT_TOKEN ""; setx TELEGRAM_CHAT_ID ""; python main.py
```

Refer to `CHECKS.md` for required env names (authoritative): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `WALLET_ADDRESS`, `ETHERSCAN_API`, `CRONOS_RPC_URL`, `TZ`.

## Common pitfalls to avoid
- Do not break the dual-signature behavior of `replay_cost_basis_over_entries` / `update_cost_basis` — many modules call the legacy path.
- Avoid introducing float-only arithmetic into ledger code; use Decimal for money and preserve serialization conventions.
- main.py contains shared global state (e.g., `_guard`, `_token_balances`) and uses threads; prefer adding locks or centralizing state in `core/runtime_state.py` for concurrency fixes.
- Telegram formatting: message length and Markdown parsing are fragile; use existing helpers instead of raw HTTP posts.
- HTTP: prefer existing `utils.http.safe_get` / `safe_json` patterns (retries/timeouts) rather than ad-hoc requests without backoff.

## Rules for AI agents editing this repo
- Read `AGENTS.md`, `MANIFEST.md`, and `CHECKS.md` before any edit.
- Preserve external behavior (APIs, JSON on-disk layout, and dual-mode call signatures) unless the change includes coordinated migration in one PR.
- Small, focused single-file PRs are preferred; include a MANIFEST entry when changing runtime behavior.
- Run tests locally and ensure imports succeed (smoke tests catch import-time regressions).

If anything in this file is unclear or you'd like me to expand examples (e.g., a small refactor checklist for `reports/ledger.py`), tell me which area to expand.
