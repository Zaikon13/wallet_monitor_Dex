# Repository State Snapshot

_Generated on 2025-10-08 00:58:48 UTC_

## state=closed / is_merged=False

| number | title | state | draft | merged | head_ref | base_ref | checks_overall | updated_at |
|--------|-------|-------|-------|--------|----------|----------|----------------|------------|
| #4 | Fix per-asset aggregation and stabilize pytest startup | closed | True | False | codex/add-@codex-functionality | main | success — build: success | 2025-09-19 17:20:59 UTC |

## state=closed / is_merged=True

| number | title | state | draft | merged | head_ref | base_ref | checks_overall | updated_at |
|--------|-------|-------|-------|--------|----------|----------|----------------|------------|
| #27 | PR-000: global dedent & import-time hygiene | closed | False | True | codex/fix-global-dedent-and-import-hygiene | main | success — build: success | 2025-10-07 22:31:17 UTC |
| #26 | Fix markdown escaping and env parsing for tests | closed | False | True | codex/verify-environment-variable-usage | main | success — build: success | 2025-10-07 16:29:58 UTC |
| #25 | fix(holdings): RPC-seed CRO + env fallback | closed | False | True | codex/replace-holdings.py-and-open-pr | main | success — build: success | 2025-10-07 15:53:53 UTC |
| #23 | fix(holdings): env fallback for wallet address | closed | False | True | codex/fix-holdings.py-to-read-multiple-env-vars | main | success — build: success | 2025-10-07 15:20:24 UTC |
| #22 | fix(holdings): seed CRO from RPC; clean conflict-free snapshot | closed | False | True | codex/create-clean-pr-for-holdings-fix | main | success — build: success | 2025-10-07 10:51:05 UTC |
| #20 | fix(holdings): always include native CRO in snapshot | closed | False | True | codex/fix-holdings-to-always-include-native-cro | main | success — build: success | 2025-10-06 20:11:44 UTC |
| #19 | recovery/batch3: functional restore (/holdings, /daily, /show) + EOD reports + residual cleanup | closed | False | True | codex/create-new-branch-recovery/batch3 | main | success — build: success | 2025-10-06 19:06:49 UTC |
| #18 | recovery/batch2: runtime recovery (no side-effects on import) + EOD scheduler + scripts hygiene | closed | False | True | codex/create-branch-recovery/batch2-from-main | main | success — lint: success; build: success | 2025-10-06 17:46:28 UTC |
| #17 | recovery/batch1: Critical Fix Pack (restore missing symbols and script hygiene) | closed | False | True | codex/create-branch-recovery/batch1-from-main | main | success — build: success | 2025-10-06 16:48:37 UTC |
| #16 | chore(ci): add Batch C import & lint | closed | False | True | ci/batch-c-lint | main | success — build: success; lint: success | 2025-10-03 02:43:14 UTC |
| #14 | feat: add Telegram reporting suite and schedulers | closed | False | True | codex/create-reporting-suite-with-telegram-commands | main | success — build: success | 2025-09-24 06:56:36 UTC |
| #13 | feat(runtime): stabilize main loop (EOD, TG loop, throttled errors, health ping) + robust Cronos parsing + Telegram dedupe/plain-text | closed | False | True | codex/implement-runtime-stabilizations-and-improvements | main | success — build: success | 2025-09-24 06:09:42 UTC |
| #11 | fix(cronos): robust tx parsing; guard non-dict items | closed | False | True | codex/harden-parsing-in-cronos.py | main | success — build: success | 2025-09-24 05:38:32 UTC |
| #10 | fix(telegram): dedupe identical messages within a short window to prevent cooldown spam | closed | False | True | codex/add-deduplication-to-send_telegram | main | success — build: success | 2025-09-24 05:37:53 UTC |
| #9 | fix: robust Cronos tx parsing (guard non-dict items) | closed | False | True | codex/implement-robust-parsing-for-cronos-txs | main | success — build: success | 2025-09-24 05:09:13 UTC |
| #8 | fix: telegram send uses safe plain text by default (no MarkdownV2) | closed | False | True | codex/create-branch-fix/telegram-escape-and-update-telegram-api | main | success — build: success | 2025-09-24 05:03:54 UTC |
| #7 | Align main imports with available helpers | closed | False | True | codex/align-and-verify-imports-in-main.py | main | success — build: success | 2025-09-24 04:38:08 UTC |
| #6 | Make signals server resilient to missing Flask | closed | False | True | codex/restore-core-files-and-create-pr | main | success — build: success | 2025-09-24 04:35:28 UTC |

## state=open / is_merged=False

| number | title | state | draft | merged | head_ref | base_ref | checks_overall | updated_at |
|--------|-------|-------|-------|--------|----------|----------|----------------|------------|
| #37 | PR-008 (meta): finalize merges + cleanup | open | True | False | codex/finalize-merges-and-cleanup-process | main | success — build: success | 2025-10-08 00:55:04 UTC |
| #36 | PR-007′ (meta): merge executor run | open | True | False | codex/execute-ci-gated-merges-for-pr-001-to-pr-003 | main | success — build: success | 2025-10-08 00:43:25 UTC |
| #35 | PR-007 (meta): Merge orchestration + runtime smoke | open | True | False | codex/merge-pr-001/002/003-if-ci-is-green | main | success — build: success | 2025-10-08 00:37:33 UTC |
| #34 | RESET-002 (meta): promote PR-001/002/003 & rebase on main | open | True | False | codex/rebase-and-promote-prs-on-main | main | success — build: success | 2025-10-08 00:30:50 UTC |
| #33 | RESET-001: Stabilize CI (AST-only) + Auto-repair | open | True | False | codex/stabilize-ci-with-ast-only-parsing | main | success — ast_only: success | 2025-10-08 00:26:38 UTC |
| #32 | PR-005: AST repair sweep + verbose CI diagnostics | open | True | False | codex/add-ast-diagnostic-and-repair-script | main | failure — ast_and_tests: failure | 2025-10-08 00:15:25 UTC |
| #31 | PR-004: ensure CI on pull_request (AST + pytest) | open | True | False | codex/post-pr-000-sync-with-ci-setup | main | failure — ast_and_tests: failure | 2025-10-08 00:05:05 UTC |
| #30 | PR-003: smoke_holdings + manual runtime-smoke workflow | open | True | False | codex/add-smoke-script-and-ci-workflow | main | success — build: success | 2025-10-07 23:37:36 UTC |
| #29 | PR-002: wire /holdings to live adapters (CRO≠tCRO, merged uPnL) | open | True | False | codex/wire-holdings-to-live-adapters | main | success — build: success | 2025-10-07 23:13:30 UTC |
| #28 | PR-001: core/holdings — CRO≠tCRO; merged uPnL; stable snapshot API | open | True | False | codex/update-core/holdings.py-with-new-features | main | success — build: success | 2025-10-07 22:57:43 UTC |
| #24 | fix(holdings): env fallback for wallet address | open | False | False | codex/add-env-fallback-for-wallet-address | main | success — build: success | 2025-10-07 15:44:36 UTC |
| #21 | fix(holdings): always include native CRO in snapshot | open | False | False | codex/fix-holdings-to-always-include-native-cro-53vv7e | main | success — build: success | 2025-10-07 10:14:43 UTC |
| #15 | Handle Telegram send errors in snapshot script | open | True | False | codex/update-snapshot_wallet.py-for-error-handling | main | success — build: success | 2025-09-30 04:21:08 UTC |
| #12 | feat(main): add EOD report, TG long-poll thread, and throttled error notify | open | True | False | codex/implement-eod-report-and-telegram-features | main | success — build: success | 2025-09-24 05:58:49 UTC |
| #5 | Fix aggregate reporting and handle missing Flask dependency | open | False | False | codex/work-on-code-in-wallet_monitor_dex | main | success — build: success | 2025-09-24 03:14:11 UTC |
