from __future__ import annotations

import time
from decimal import Decimal

import pytest

from core.runtime_state import note_tick, note_wallet_poll, update_snapshot
from telegram import commands as cmd


@pytest.fixture(autouse=True)
def reset_runtime_state():
    update_snapshot({}, time.time())
    note_wallet_poll(True)
    note_tick()
    yield


def _sample_entries():
    return [
        {
            "wallet": "0xabc",
            "asset": "ABC",
            "side": "IN",
            "qty": Decimal("10"),
            "usd": Decimal("100"),
            "realized_usd": Decimal("0"),
            "time": int(time.time()),
        },
        {
            "wallet": "0xabc",
            "asset": "ABC",
            "side": "OUT",
            "qty": Decimal("4"),
            "usd": Decimal("60"),
            "realized_usd": Decimal("20"),
            "time": int(time.time()),
        },
        {
            "wallet": "0xabc",
            "asset": "XYZ",
            "side": "IN",
            "qty": Decimal("5"),
            "usd": Decimal("25"),
            "realized_usd": Decimal("0"),
            "time": int(time.time()),
        },
    ]


def test_command_lengths(monkeypatch):
    entries = _sample_entries()

    monkeypatch.setattr(cmd, "iter_all_entries", lambda: iter(entries))
    monkeypatch.setattr(cmd, "read_ledger", lambda _day: entries)
    monkeypatch.setattr(cmd, "build_day_report_text", lambda **_kw: "daily text")
    monkeypatch.setattr(cmd, "build_weekly_report_text", lambda **_kw: "weekly text")

    update_snapshot({"ABC": {"qty": "10", "usd": "100"}}, time.time())
    note_wallet_poll(True)
    note_tick()

    outputs = [
        cmd.handle_diag(),
        cmd.handle_status(),
        cmd.handle_holdings(),
        cmd.handle_totals(),
        cmd.handle_daily(),
        cmd.handle_weekly(),
        cmd.handle_pnl("ABC"),
        cmd.handle_pnl(None),
        cmd.handle_tx("ABC"),
        cmd.handle_tx(None),
    ]

    for text in outputs:
        assert isinstance(text, str)
        assert len(text) <= 4096
