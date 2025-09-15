# core/holdings.py
# Compute wallet holdings (qty + MTM in USD) using RPC + pricing
from __future__ import annotations
import os
from typing import Tuple, List, Dict
from core.rpc import rpc_init, get_native_balance, erc20_balance, erc20_symbol
from core.pricing import get_price_usd

WALLET = (os.getenv("WALLET_ADDRESS", "")).lower()
TOKENS = [x.strip().lower() for x in os.getenv("TOKENS", "").split(",") if x.strip()]


def compute_holdings() -> Tuple[float, List[Dict]]:
    """Return (total_usd, breakdown) where breakdown has
    [{token, token_addr, amount, price_usd, usd_value}] sorted by usd_value desc.
    CRO native always included if > 0.
    ERC20s taken from `TOKENS` env (cronos/0x..).
    """
    total = 0.0
    breakdown: List[Dict] = []

    # Native CRO
    if WALLET and rpc_init():
        cro_amt = get_native_balance(WALLET)
    else:
        cro_amt = 0.0
    if cro_amt > 0:
        cro_px = get_price_usd("CRO") or 0.0
        cro_val = cro_amt * cro_px
        breakdown.append({
            "token": "CRO",
            "token_addr": None,
            "amount": cro_amt,
            "price_usd": cro_px,
            "usd_value": cro_val,
        })
        total += cro_val

    # ERC20s (explicit list from env for stability)
    for item in TOKENS:
        if not item.startswith("cronos/"):
            continue
        _, addr = item.split("/", 1)
        if not addr.startswith("0x"):
            continue
        amt = erc20_balance(addr, WALLET)
        if amt <= 0:
            continue
        sym = erc20_symbol(addr)
        px = get_price_usd(addr) or 0.0
        val = amt * px
        breakdown.append({
            "token": sym or addr[:8].upper(),
            "token_addr": addr,
            "amount": amt,
            "price_usd": px,
            "usd_value": val,
        })
        total += val

    breakdown.sort(key=lambda b: float(b.get("usd_value", 0.0)), reverse=True)
    return total, breakdown
