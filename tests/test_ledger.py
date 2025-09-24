from __future__ import annotations

import importlib
from decimal import Decimal


def test_cost_basis_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    ledger = importlib.reload(importlib.import_module("reports.ledger"))

    buy_entry = {
        "wallet": "0xabc",
        "time": 1700000000,
        "side": "IN",
        "asset": "ABC",
        "qty": "10",
        "usd": "100",
    }
    sell_entry = {
        "wallet": "0xabc",
        "time": 1700003600,
        "side": "OUT",
        "asset": "ABC",
        "qty": "4",
        "usd": "80",
    }

    ledger.append_ledger("2023-11-14", buy_entry)
    ledger.append_ledger("2023-11-15", sell_entry)

    ledger.update_cost_basis()

    day1 = ledger.read_ledger("2023-11-14")
    day2 = ledger.read_ledger("2023-11-15")

    assert day1[0]["realized_usd"] == Decimal("0")
    assert day2[0]["realized_usd"].quantize(Decimal("0.01")) == Decimal("40.00")

    basis_file = ledger.COST_BASIS_FILE
    assert basis_file.exists()
