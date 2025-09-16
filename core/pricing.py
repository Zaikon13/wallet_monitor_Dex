from __future__ import annotations
import os, time
from typing import Any, Dict, Optional

HISTORY_LAST_PRICE: Dict[str, float] = {}

try:
    from utils.http import safe_get, safe_json
except ImportError:
    import requests
    def safe_get(url: str, params=None, timeout: int = 10, retries: int = 1, backoff: float = 1.0):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            return r if r.ok else None
        except Exception:
            return None
    def safe_json(response: Any) -> Any:
        if response is None:
            return None
        try:
            return response.json()
        except Exception:
            return None

_price_cache: Dict[str, tuple[float, float]] = {}
_CACHE_TTL = 60.0

DEX_BASE_TOKENS = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH = "https://api.dexscreener.com/latest/dex/search"

def _cache_get(key: str) -> Optional[float]:
    item = _price_cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > _CACHE_TTL:
        return None
    return val

def _cache_set(key: str, value: float) -> None:
    _price_cache[key] = (time.time(), value)

def _pick_best_pair(pairs: list, chain: str = "cronos") -> Optional[dict]:
    best_pair = None
    best_liq = -1.0
    for p in pairs or []:
        try:
            if str(p.get("chainId", "")).lower() != chain.lower():
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            price = float(p.get("priceUsd") or 0)
            if price <= 0:
                continue
            if liq > best_liq:
                best_liq = liq
                best_pair = p
        except Exception:
            continue
    return best_pair

def get_price_usd(asset: str, chain: str = "cronos") -> float:
    """
    Returns the current USD price for the given asset (symbol or contract address).
    """
    if not asset:
        return 0.0
    key = asset.strip().lower()
    cached = _cache_get(f"{chain}:{key}")
    if cached is not None:
        return cached
    price: Optional[float] = None
    try:
        if key in ("cro", "wcro", "w-cro", "wrappedcro", "wrapped cro"):
            for q in ("wcro usdt", "wcro usdc", "cro usdt"):
                data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10))
                if data:
                    p = _pick_best_pair(data.get("pairs") or [], chain=chain)
                    if p:
                        p_price = float(p.get("priceUsd") or 0)
                        if p_price > 0:
                            price = p_price
                            break
        elif key.startswith("0x") and len(key) == 42:
            data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/{chain}/{key}", timeout=10))
            pairs = data.get("pairs") if data else []
            if not pairs:
                data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": key}, timeout=10))
                pairs = data.get("pairs") if data else []
            if pairs:
                p = _pick_best_pair(pairs, chain=chain)
                if p:
                    p_price = float(p.get("priceUsd") or 0)
                    if p_price > 0:
                        price = p_price
        else:
            data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": key}, timeout=10))
            pairs = data.get("pairs") if data else []
            if pairs:
                p = _pick_best_pair(pairs, chain=chain)
                if p:
                    p_price = float(p.get("priceUsd") or 0)
                    if p_price > 0:
                        price = p_price
            if price is None and len(key) <= 12:
                for q in (f"{key} usdt", f"{key} wcro"):
                    data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": q}, timeout=10))
                    pairs = data.get("pairs") if data else []
                    if pairs:
                        p = _pick_best_pair(pairs, chain=chain)
                        if p:
                            p_price = float(p.get("priceUsd") or 0)
                            if p_price > 0:
                                price = p_price
                                break
    except Exception:
        price = None
    if price is None or (isinstance(price, float) and price <= 0):
        hist_price = None
        if key.startswith("0x"):
            hist_price = HISTORY_LAST_PRICE.get(key.upper()) or HISTORY_LAST_PRICE.get(key)
        else:
            hist_price = HISTORY_LAST_PRICE.get(asset.upper())
        if hist_price and hist_price > 0:
            price = float(hist_price)
    if price is None:
        price = 0.0
    _cache_set(f"{chain}:{key}", price)
    if not key.startswith("0x"):
        HISTORY_LAST_PRICE[asset.upper()] = price
    return price

def get_change_and_price_for_symbol_or_addr(sym_or_addr: str, chain: str = "cronos") -> tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """
    Returns (price_usd, 24h_change_pct, 2h_change_pct, dexscreener_url) for the asset.
    """
    if not sym_or_addr:
        return (None, None, None, None)
    query = sym_or_addr.strip()
    if query.lower() in ("cro", "wcro"):
        price = get_price_usd("CRO", chain=chain)
        return (price if price else None, None, None, None)
    pairs = []
    if query.lower().startswith("0x") and len(query) == 42:
        data = safe_json(safe_get(f"{DEX_BASE_TOKENS}/{chain}/{query}", timeout=12))
        pairs = data.get("pairs") if data else []
        if not pairs:
            data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=12))
            pairs = data.get("pairs") if data else []
    else:
        data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": query}, timeout=12))
        pairs = data.get("pairs") if data else []
    if not pairs:
        return (None, None, None, None)
    best = _pick_best_pair(pairs, chain=chain)
    if not best:
        return (None, None, None, None)
    try:
        price = float(best.get("priceUsd") or 0)
    except Exception:
        price = 0.0
    ch24 = None
    ch2h = None
    try:
        changes = best.get("priceChange") or {}
        if "h24" in changes:
            ch24 = float(changes.get("h24"))
        if "h2" in changes:
            ch2h = float(changes.get("h2"))
    except Exception:
        ch24 = None
        ch2h = None
    pair_address = best.get("pairAddress")
    url = f"https://dexscreener.com/{chain}/{pair_address}" if pair_address else None
    return (price if price and price > 0 else None, ch24, ch2h, url)
