# core/pricing.py
from __future__ import annotations
import time
from typing import Dict, Tuple, Optional, List
from utils.http import safe_get, safe_json

# Dexscreener endpoints
DEX_BASE_TOKENS  = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH  = "https://api.dexscreener.com/latest/dex/search"

# Common symbol aliases
PRICE_ALIASES: Dict[str, str] = {"tcro": "cro"}

# Exposed history map so other modules (π.χ. main.py) μπορούν να το seed-άρουν με last-seen prices.
HISTORY_LAST_PRICE: Dict[str, float] = {}

# Simple price cache (seconds)
PRICE_CACHE: Dict[str, Tuple[Optional[float], float]] = {}
PRICE_CACHE_TTL = 60.0


def _pick_best_price(pairs: List[dict] | None) -> Optional[float]:
    if not pairs:
        return None
    best, best_liq = None, -1.0
    for p in pairs:
        try:
            if str(p.get("chainId", "")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq, best = liq, price
        except:  # noqa: E722
            continue
    return best


def _pairs_for_token_addr(addr: str) -> List[dict]:
    data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/cronos/{addr}", timeout=10)) or {}
    pairs = data.get("pairs") or []
    if not pairs:
        data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/{addr}", timeout=10)) or {}
        pairs = data.get("pairs") or []
    if not pairs:
        data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": addr}, timeout=10)) or {}
        pairs = data.get("pairs") or []
    return pairs


def _history_price_fallback(query_key: str, symbol_hint: str | None = None) -> Optional[float]:
    if not query_key:
        return None
    k = query_key.strip()
    if not k:
        return None
    # First try exact 0x address key
    if k.startswith("0x"):
        p = HISTORY_LAST_PRICE.get(k)
        if p and p > 0:
            return p
    # Then try symbol (upper + alias)
    sym = (symbol_hint or k)
    sym = (PRICE_ALIASES.get(sym.lower(), sym.lower())).upper()
    p = HISTORY_LAST_PRICE.get(sym)
    if p and p > 0:
        return p
    return None


def get_price_usd(symbol_or_addr: str) -> Optional[float]:
    """
    Returns best USD price for a Cronos token (symbol or 0x address).
    Uses Dexscreener and falls back to HISTORY_LAST_PRICE when needed.
    """
    if not symbol_or_addr:
        return None
    key = PRICE_ALIASES.get(symbol_or_addr.strip().lower(), symbol_or_addr.strip().lower())
    now = time.time()
    c = PRICE_CACHE.get(key)
    if c and (now - c[1] < PRICE_CACHE_TTL):
        return c[0]

    price = None
    try:
        # CRO shortcuts
        if key in ("cro", "wcro", "w-cro", "wrappedcro", "wrapped cro"):
            for q in ["wcro usdt", "cro usdt", "cro usdc"]:
                data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
                price = _pick_best_price(data.get("pairs"))
                if price:
                    break
        # 0x address
        elif key.startswith("0x") and len(key) == 42:
            price = _pick_best_price(_pairs_for_token_addr(key))
        # symbol
        else:
            for q in [key, f"{key} usdt", f"{key} wcro"]:
                data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10)) or {}
                price = _pick_best_price(data.get("pairs"))
                if price:
                    break
    except:  # noqa: E722
        price = None

    # Fallback σε τελευταία ιστορική τιμή από ledger
    if (price is None) or (not price) or (float(price) <= 0):
        hist = _history_price_fallback(symbol_or_addr, symbol_hint=symbol_or_addr)
        if hist and hist > 0:
            price = float(hist)

    PRICE_CACHE[key] = (price, now)
    return price
