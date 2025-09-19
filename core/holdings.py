# core/holdings.py
"""
Wallet holdings snapshot helpers.
- Merge RPC balances with historical ledger if needed
- Return clean dict: {symbol: {"amount": Decimal, "usd_value": Decimal}}
"""

import logging
from decimal import Decimal
from core.rpc import get_wallet_balances
from core.pricing import get_price_usd


def get_wallet_snapshot(wallet_address: str, include_usd: bool = True) -> dict:
    """
    Take a snapshot of wallet holdings (by calling RPC).
    Returns dict: {symbol: {"amount": Decimal, "usd_value": Decimal}}
    """
    snapshot = {}
    try:
        balances = get_wallet_balances(wallet_address)
        for symbol, amount in balances.items():
            amt = Decimal(str(amount))
            entry = {"amount": amt}
            if include_usd:
                try:
                    price = get_price_usd(symbol)
                    entry["usd_value"] = amt * Decimal(str(price))
                except Exception as e:
                    logging.warning(f"Price fetch failed for {symbol}: {e}")
                    entry["usd_value"] = Decimal("0")
            snapshot[symbol] = entry
    except Exception as e:
        logging.exception("Failed to build wallet snapshot")
    return snapshot


def format_snapshot_lines(snapshot: dict) -> list[str]:
    """
    Convert snapshot dict into list of formatted text lines for Telegram output.
    """
    lines = []
    for symbol, data in snapshot.items():
        amt = data.get("amount", Decimal(0))
        usd = data.get("usd_value", Decimal(0))
        lines.append(f"{symbol}: {amt:.6f} ≈ ${usd:.2f}")
    return lines
