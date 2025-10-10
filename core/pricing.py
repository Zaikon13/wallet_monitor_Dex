# -*- coding: utf-8 -*-
"""
core/pricing.py — Spot USD pricing helpers for holdings snapshot.

Public API expected by core/holdings.py:
- get_spot_usd(symbol: str, address: str | None = None) -> Decimal | None
- (optional) get_symbol_for_address(address: str) -> str | None

Strategy:
1) Normalize common wrappers (tCRO/WCRO → CRO).
2) CoinGecko Simple Price (primary).
3) Lightweight in-process cache to limit calls.
"""

from __future__ import annotations
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict

import requests

# --- In-process cache (symbol → (ts, price))
_PRICE_CACHE: Dict[str, tuple[float, Decimal]] = {}
_CACHE_TTL = float(os.getenv("PRICING_CACHE_TTL", "60"))  # seconds

# Map known symbols → CoinGecko IDs
_COINGECKO_IDS = {
    "CRO": "crypto-com-chain",
    # Add more here if θέλεις (USDC/USDT/ETH/WBTC κ.λπ.)
    # "USDC": "usd-coin",
    # "USDT": "tether",
    # "ETH": "ethereum",
    # "WBTC": "wrapped-bitcoin",
}

def _now() -> float:
    return time.time()

def _to_decimal(x: object) -> Optional[Decimal]:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None

def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if s in {"TCRO", "WCRO", "WCRO-RECEIPT"}:
        return "CRO"
    return s or "?"

def _cg_simple_price(coin_id: str) -> Optional[Decimal]:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        usd = data.get(coin_id, {}).get("usd")
        return _to_decimal(usd)
    except Exception:
        return None

def _cg_id_for_symbol(symbol: str) -> Optional[str]:
    return _COINGECKO_IDS.get(symbol.upper())

def get_symbol_for_address(address: str) -> Optional[str]:
    """
    Προαιρετικό helper που μπορεί να χρησιμοποιηθεί από το holdings για ERC-20 discovery.
    Προς το παρόν δεν διατηρούμε mapping συμβολαίου→symbol, επέστρεψε None.
    """
    return None

def get_spot_usd(symbol: str, address: Optional[str] = None) -> Optional[Decimal]:
    """
    Spot USD price for a token symbol (normalized). Returns None on failure.
    """
    sym = _norm_symbol(symbol)

    # Cache
    hit = _PRICE_CACHE.get(sym)
    if hit and (_now() - hit[0] <= _CACHE_TTL):
        return hit[1]

    price: Optional[Decimal] = None

    # Primary: CoinGecko
    coin_id = _cg_id_for_symbol(sym)
    if coin_id:
        price = _cg_simple_price(coin_id)

    # TODO: Add fallbacks (Dexscreener/CryptoCompare) if needed.

    if price is not None:
        _PRICE_CACHE[sym] = (_now(), price)
    return price
