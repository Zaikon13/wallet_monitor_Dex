# -*- coding: utf-8 -*-
"""
reports/ledger.py — minimal stub for avg cost.

The holdings snapshot will call:
    get_avg_cost_usd(symbol: str) -> Decimal | None

Return None to indicate "no ledger/cost available yet".
This keeps the flow stable without throwing import/attr errors.
"""

from __future__ import annotations
from decimal import Decimal
from typing import Optional

def get_avg_cost_usd(symbol: str) -> Optional[Decimal]:
    # TODO: Wire up real ledger average cost when your trade history is ready.
    return None
