from __future__ import annotations

import importlib

# Basic module import sanity — no network, no side effects expected
MODULES = [
    "utils.http",
    "core.tz",
    "core.config",
    "core.pricing",
    "core.holdings",
    "core.runtime_state",
    "core.guards",
    "core.watch",
    "core.wallet_monitor",
    "reports.aggregates",
    "reports.ledger",
    "reports.day_report",
    "reports.weekly",
    "telegram.api",
    "telegram.formatters",
    "telegram.dispatcher",
    "telegram.commands",
]


def test_imports():
    for m in MODULES:
        importlib.import_module(m)
