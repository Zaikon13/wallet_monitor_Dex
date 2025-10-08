# -*- coding: utf-8 -*-
"""
Adapters layer for holdings: builds a normalized snapshot used by Telegram commands
and smoke scripts. No network side-effects at import time.
"""
from __future__ import annotations
from typing import Any, Dict
try:
    from core.holdings import get_wallet_snapshot  # authoritative API
except Exception:
    # Fallback no-op to avoid import-time crash in CI
    def get_wallet_snapshot(base_ccy: str = "USD", limit: int = 9999) -> Dict[str, Any]:
        return {"assets": [], "totals": {"value_usd": "0", "cost_usd": "0", "u_pnl_usd": "0", "u_pnl_pct": "0"}}

def build_holdings_snapshot(base_ccy: str = "USD", limit: int = 9999) -> Dict[str, Any]:
    """
    Thin adapter used by main.py/telegram. Delegates to core.holdings.
    """
    snap = get_wallet_snapshot(base_ccy=base_ccy, limit=limit)
    # Ensure required keys exist (defensive against partial fallbacks)
    snap.setdefault("assets", [])
    snap.setdefault("totals", {}).setdefault("value_usd", "0")
    snap["totals"].setdefault("cost_usd", "0")
    snap["totals"].setdefault("u_pnl_usd", "0")
    snap["totals"].setdefault("u_pnl_pct", "0")
    return snap
