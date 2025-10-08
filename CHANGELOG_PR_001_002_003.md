# Changelog — Holdings path

This log captures the coordinated work that landed through PR-001, PR-002, and PR-003.
Each section lists the high-impact changes that shipped once the merge stack was
finalized.

## PR-001 · core/holdings — deterministic wallet snapshot

- Rebuilt `core.holdings` around a dedicated `AssetSnap` dataclass so holdings,
  totals, and PnL figures remain `Decimal`-precise through the full pipeline.
- Normalized CRO aliases (tCRO/TCRO/WCRO-receipt) and merged unrealized PnL while
  still surfacing separate symbols for reporting downstream.
- Added guarded RPC discovery for ERC-20 contracts plus native CRO fallbacks so a
  missing adapter still returns a meaningful snapshot instead of crashing the CLI.
- Wired optional ledger integration by pulling average cost from
  `reports.ledger.get_avg_cost_usd`, keeping import-time side effects disabled.

## PR-002 · adapters + surfaces — main/Telegram wiring

- Introduced `core.holdings_adapters.build_holdings_snapshot` as the single
  entrypoint used by runtime surfaces, providing resilient defaults when
  `core.holdings` is unavailable in CI.
- Updated `main.py` to load `.env`, respect `DRY_RUN`, and render holdings via the
  adapter with safe stdout fallbacks when Telegram or the adapter is missing.
- Refreshed `telegram.commands.holdings` so chat commands reuse the adapter,
  prefer the dedicated formatter, and emit totals with merged unrealized PnL.
- Added a minimal holdings smoke script for CI (`scripts/smoke_holdings_text.py`)
  plus import-only pytest smoke coverage to ensure the new surface stays green.

## PR-003 · smoke workflows + runtime guardrails

- Added a `runtime-smoke` GitHub Actions workflow that installs dependencies,
  exports safe environment defaults, and runs the CLI with `DRY_RUN=1` to catch
  scheduler/Telegram regressions without touching the network.
- Kept the lightweight AST-only workflow (`.github/workflows/ci.yml`) as the
  canonical PR gate, while allowing optional smoke runs to be triggered manually.
- Documented the new smoke entrypoints so operations can verify holdings output
  without waiting for a production deploy.

## Meta cleanup

- Removed merge-orchestration breadcrumbs from `.github/` now that the PR stack
  has landed (PR-007/PR-008/RESET-002 notes and CI nudges).
- Updated the project manifest to mark the merge train as delivered and to
  capture post-merge follow-up items in one place.
