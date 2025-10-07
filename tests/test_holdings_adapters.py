# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import types


def test_imports():
    mod = importlib.import_module("core.holdings_adapters")
    assert isinstance(mod, types.ModuleType)
    assert hasattr(mod, "build_holdings_snapshot")


def test_snapshot_schema():
    mod = importlib.import_module("core.holdings_adapters")
    snap = mod.build_holdings_snapshot(base_ccy="USD")
    assert "assets" in snap and isinstance(snap["assets"], list)
    assert "totals" in snap and isinstance(snap["totals"], dict)
    # if assets exist, check keys
    if snap["assets"]:
        a0 = snap["assets"][0]
        for k in ("symbol","amount","price_usd","value_usd","u_pnl_usd","u_pnl_pct"):
            assert k in a0
