# core/pricing.py
# Dexscreener-based lightweight pricing with small in-memory cache.
from __future__ import annotations
import time
from typing import Optional
from utils.http import safe_get, safe_json

DEX_BASE_SEARCH = "https://api.dexscreener.com/latest/dex/search"

# simple TTL cache: key -> (price, ts)
_PRICE_CACHE: dict[str, tuple[Optional[float], float]] = {}
PRICE_CACHE_TTL = 60  # seconds

CANONICAL_WCRO_QUERIES = ("wcro usdt", "wcro usdc", "cro usdt")


def _pick_best_price(pairs: list[dict]) -> Optional[float]:
    if not pairs:
        return None
    best_price, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId", "")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq, best_price = liq, price
        except Exception:
            continue
    return best_price


def _search_pairs(query: str) -> list[dict]:
    data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=12)) or {}
    return data.get("pairs") or []


def get_price_usd(sym_or_addr: str) -> Optional[float]:
    """Return price in USD (Dexscreener), CRO via WCRO route. None if unknown."""
    if not sym_or_addr:
        return None
    key = sym_or_addr.strip().lower()
    now = time.time()

    cached = _PRICE_CACHE.get(key)
    if cached and (now - cached[1] < PRICE_CACHE_TTL):
        return cached[0]

    price = None
    try:
        if key in ("cro", "wcro", "wrapped cro", "wrappedcro", "w-cro"):
            for q in CANONICAL_WCRO_QUERIES:
                p = _pick_best_price(_search_pairs(q))
                if p and p > 0:
                    price = p
                    break
        elif key.startswith("0x") and len(key) == 42:
            price = _pick_best_price(_search_pairs(key))
        else:
            # symbol lookup + symbol/USDT and symbol/WCRO fallbacks
            for q in (key, f"{key} usdt", f"{key} wcro"):
                p = _pick_best_price(_search_pairs(q))
                if p and p > 0:
                    price = p
                    break
    except Exception:
        price = None

    _PRICE_CACHE[key] = (price, now)
    return price
