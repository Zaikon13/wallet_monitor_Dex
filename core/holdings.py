# core/holdings.py
from decimal import Decimal
from core.rpc import get_wallet_balances

def get_wallet_snapshot(wallet_address: str, normalize_cro: bool = True):
    raw = get_wallet_balances(wallet_address)
    results = []

    for item in raw:
        symbol = item.get("symbol")
        balance = Decimal(item.get("balance", 0))
        decimals = int(item.get("decimals", 18))

        if balance == 0 or not symbol:
            continue

        # Normalize balance
        norm_balance = balance / Decimal(10 ** decimals)

        # Normalize tCRO, WCRO to CRO
        if normalize_cro and symbol in ["tCRO", "WCRO"]:
            symbol = "CRO"

        results.append({
            "symbol": symbol,
            "balance": norm_balance.quantize(Decimal("0.0001")),
        })

    return results
