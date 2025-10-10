# -*- coding: utf-8 -*-
"""
core/pricing.py — Spot USD pricing helpers for holdings snapshot.

API used by core/holdings.py:
- get_spot_usd(symbol: str, token_address: str | None = None) -> Decimal | None
- (optional) get_symbol_for_address(address: str) -> str | None

Strategy:
1) Normalize wrappers (tCRO/WCRO -> CRO).
2) CoinGecko Simple Price (primary; symbol->id map).
3) Fallback: Dexscreener by token_address (Cronos-friendly).
4) In-process cache to limit calls.
"""

from __future__ import annotations
import os
import time
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, Any, List

import requests

_PRICE_CACHE: Dict[str, tuple[float, Decimal]] = {}
_CACHE_TTL = float(os.getenv("PRICING_CACHE_TTL", "60"))

_COINGECKO_IDS = {
    "CRO": "crypto-com-chain",
    "XRP": "ripple",
    "WETH": "weth",
    "SUI": "sui",
    "WBTC": "wrapped-bitcoin",
    "SOL": "solana",
    "ADA": "cardano",
    "USDT": "tether",
    "HBAR": "hedera-hashgraph",
    "DOGE": "dogecoin",
    "JASMY": "jasmycoin",
    "XYO": "xyo-network",
    # add more symbols here as you need them
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

def _cache_get(key: str) -> Optional[Decimal]:
    hit = _PRICE_CACHE.get(key)
    if not hit:
        return None
    ts, val = hit
    if (_now() - ts) <= _CACHE_TTL:
        return val
    return None

def _cache_set(key: str, val: Decimal) -> None:
    _PRICE_CACHE[key] = (_now(), val)

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

def _dex_price_by_token_address(token_address: str) -> Optional[Decimal]:
    if not token_address:
        return None
    cached = _cache_get(f"addr:{token_address.lower()}")
    if cached is not None:
        return cached
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        pairs: List[Dict[str, Any]] = data.get("pairs") or []
        if not pairs:
            return None
        candidates = [p for p in pairs if p.get("priceUsd")]
        if not candidates:
            return None
        best = max(candidates, key=lambda p: float(p.get("liquidity", {}).get("usd", 0.0)))
        price = _to_decimal(best.get("priceUsd"))
        if price is not None:
            _cache_set(f"addr:{token_address.lower()}", price)
        return price
    except Exception:
        return None

def get_symbol_for_address(address: str) -> Optional[str]:
    # Optional mapping (not maintained here)
    return None

def get_spot_usd(symbol: str, token_address: Optional[str] = None) -> Optional[Decimal]:
    sym = _norm_symbol(symbol)

    cached = _cache_get(f"sym:{sym}")
    if cached is not None:
        return cached

    price: Optional[Decimal] = None
    coin_id = _cg_id_for_symbol(sym)
    if coin_id:
        price = _cg_simple_price(coin_id)

    if price is None and token_address:
        price = _dex_price_by_token_address(token_address)

    if price is not None:
        _cache_set(f"sym:{sym}", price)
    return price
