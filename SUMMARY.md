version: 1
generated_at: "2025-09-13T15:01:21Z"

## Summary
Double-counted balances: compute_holdings_merged blindly sums RPC and history amounts, risking inflated holdings and PnL

CRO/TCRO price ambiguity: get_price_usd aliases TCRO→CRO and searches multiple pairs without enforcing the canonical WCRO/USDT route, leaving room for mispricing

Unsynchronized global state: token balances, cost basis, and guard maps mutate from multiple threads without locking, enabling race conditions and ledger corruption

Weak HTTP resilience: safe_get lacks exception handling, jitter, or circuit-breaker logic, so transient failures bubble up and may stall loops

Telegram formatting risks: send_telegram posts raw Markdown and ignores message length limits, causing malformed or dropped alerts

Floating-point PnL: update_cost_basis relies on floats and average cost, omitting fees and FIFO/LIFO configuration, which skews realized results

Incomplete per-asset aggregates: aggregate_per_asset omits net_qty, net_usd, and tx_count, so totals used in reports/tests are inconsistent

Long-poll robustness: telegram_long_poll_loop lacks Markdown escaping, durable offsets, and exponential backoff, leading to lost commands during API hiccups

Guard window drift: _guard dictionary is shared across threads without locks and trailing-stop math lacks entry/peak validation, risking missed stop alerts

Sparse tests: only smoke tests exist; no coverage for CRO/WCRO/TCRO distinctions, cost-basis replay, or alert cooldown logic

## Quick wins (≤1h)
- Add exception handling and jittered retry to safe_get.
- Escape Markdown and chunk long messages in send_telegram.
- Persist Telegram long-poll offset and add backoff.

## Must-fix before prod
- Reconcile RPC vs history in compute_holdings_merged to prevent double counting.
- Guard all shared balance/guard structures with thread-safe access.
- Replace float-based cost-basis with deterministic Decimal/FIFO logic.
