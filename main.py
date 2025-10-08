#!/usr/bin/env python3
"""Lightweight entrypoint for Cronos DeFi Sentinel.

This module keeps import-time side effects to a minimum so that it can be
safely imported in unit tests or other tooling. The actual runtime wiring lives
inside functions that are invoked explicitly by :func:`main`.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

try:  # Optional dependency used in deployments
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is optional
    load_dotenv = None  # type: ignore

# --- PR-011 safety toggles -------------------------------------------------
DRY_RUN = os.getenv("DRY_RUN", "0") in ("1", "true", "True", "yes", "on")

try:  # Adapter is optional in some test environments
    from core.holdings_adapters import build_holdings_snapshot
except Exception:  # pragma: no cover - adapter may be missing locally
    build_holdings_snapshot = None  # type: ignore

try:  # Telegram is optional; fall back to stdout if absent
    from telegram.api import send_telegram
except Exception:  # pragma: no cover - telegram disabled in tests
    send_telegram = None  # type: ignore

logger = logging.getLogger("wallet-monitor.main")


def _json_dump(data: Any) -> str:
    """Pretty JSON for debugging / stdout fallbacks."""

    try:
        return json.dumps(data, indent=2, sort_keys=True, default=str)
    except TypeError:
        return json.dumps(str(data))


def _format_currency(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except Exception:
        return str(value)


def _format_quantity(value: Any) -> str:
    try:
        amount = float(value)
    except Exception:
        return str(value)
    if abs(amount) >= 1:
        return f"{amount:,.4f}"
    if abs(amount) >= 0.0001:
        return f"{amount:.6f}"
    return f"{amount:.8f}"


def _timestamp_heading(now: Optional[datetime] = None) -> str:
    current = (now or datetime.now(timezone.utc)).astimezone()
    return current.strftime("%Y-%m-%d %H:%M:%S %Z")


def _sorted_assets(assets: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    def _value(item: Mapping[str, Any]) -> float:
        try:
            return float(item.get("value_usd") or 0.0)
        except Exception:
            return 0.0

    return sorted((dict(asset) for asset in assets), key=_value, reverse=True)


def render_holdings_text(limit: int = 20) -> str:
    """Return a human readable holdings summary.

    When the adapter is missing, a friendly warning is returned instead of
    raising, making the CLI resilient for dry runs.
    """

    if not build_holdings_snapshot:
        return "⚠️ holdings snapshot not available"

    try:
        snapshot = build_holdings_snapshot(base_ccy="USD")
    except Exception as exc:  # pragma: no cover - adapter-level errors
        return f"⚠️ failed to build holdings snapshot: {exc}"

    assets = _sorted_assets(snapshot.get("assets", []))[: max(1, limit)]
    totals = dict(snapshot.get("totals", {}))

    lines = [f"📊 Holdings — {_timestamp_heading()}"]
    if not assets:
        lines.append("No assets tracked.")
    for asset in assets:
        symbol = str(asset.get("symbol") or "?").upper()
        quantity = _format_quantity(asset.get("amount") or asset.get("qty"))
        price = _format_currency(asset.get("price_usd", "0"))
        value = _format_currency(asset.get("value_usd", "0"))
        pnl_val = _format_currency(asset.get("u_pnl_usd", "0"))
        pnl_pct = asset.get("u_pnl_pct", "0")
        lines.append(
            f"{symbol}: qty={quantity}  px=${price}  val=${value}  Δ=${pnl_val} ({pnl_pct}%)"
        )

    if totals:
        totals_value = _format_currency(totals.get("value_usd", "0"))
        totals_cost = _format_currency(totals.get("cost_usd", "0"))
        totals_pnl = _format_currency(totals.get("u_pnl_usd", "0"))
        totals_pct = totals.get("u_pnl_pct", "0")
        lines.append(
            f"— Totals: val=${totals_value}  cost=${totals_cost}  Δ=${totals_pnl} ({totals_pct}%)"
        )
    else:
        lines.append("— Totals unavailable.")

    metadata = {key: snapshot.get(key) for key in ("base_ccy", "as_of") if key in snapshot}
    if metadata:
        lines.append("")
        lines.append(_json_dump(metadata))

    return "\n".join(lines)


def _should_send_telegram() -> bool:
    return bool(send_telegram) and not DRY_RUN


def send_holdings(limit: int = 20) -> None:
    """Send holdings snapshot via Telegram or stdout fallback."""

    text = render_holdings_text(limit=limit)
    if _should_send_telegram():
        try:
            send_telegram(text)
            logger.info("Holdings snapshot sent via Telegram (%s entries)", limit)
            return
        except Exception:
            logger.exception("Failed to deliver holdings snapshot via Telegram")
    print(text)


def start_scheduler() -> None:
    """Configure scheduler jobs without triggering at import time."""

    try:
        import schedule  # type: ignore
    except Exception:  # pragma: no cover - schedule optional in tests
        logger.debug("schedule module not available; skipping scheduler setup")
        return

    try:
        schedule.clear("holdings")
    except Exception:
        # Older versions of schedule might not support tags
        pass

    try:
        schedule.every().hour.do(send_holdings, 20).tag("holdings")
        logger.info("Scheduled hourly holdings snapshot job")
    except Exception:  # pragma: no cover - scheduler failures should not crash
        logger.exception("Failed to schedule holdings snapshot job")

    if DRY_RUN:
        logger.info("DRY_RUN enabled; skipping network-heavy scheduled jobs")
        return

    # Additional network-bound jobs can be registered here guarded by DRY_RUN.


def app_boot() -> None:
    """Perform lightweight startup initialisation."""

    if load_dotenv is not None:
        try:
            load_dotenv()
        except Exception:  # pragma: no cover - dotenv is best effort
            logger.debug("dotenv load failed", exc_info=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    logger.info("Cronos DeFi Sentinel booting%s", " [DRY_RUN]" if DRY_RUN else "")

    if DRY_RUN:
        logger.info("DRY_RUN active: network calls will be skipped where possible")


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point that coordinates boot, scheduler, and snapshot output."""

    _ = argv  # Reserved for future argument parsing
    app_boot()

    try:
        start_scheduler()
    except Exception:  # pragma: no cover - scheduler wiring is optional
        logger.exception("Scheduler setup failed")

    try:
        send_holdings(limit=20)
    except Exception:  # pragma: no cover - snapshot errors should not crash CLI
        logger.exception("Holdings snapshot generation failed")

    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    import sys as _sys

    raise SystemExit(main(_sys.argv[1:]))
