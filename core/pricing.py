# core/pricing.py
# Lightweight Dexscreener pricing + in-memory cache
from __future__ import annotations
import os
import time
from typing import Any, Dict, Optional

# --- Public cache (ζητείται από main.py) ---
HISTORY_LAST_PRICE: Dict[str, float] = {}  # π.χ. {"CRO": 0.12, "0xabc...": 0.00123}

# --- Προαιρετικά helpers από utils.http, αλλιώς fallback σε requests ---
try:
    from utils.http import get_json as _get_json  # type: ignore
except Exception:
    import requests

    def _get_json(url: str, timeout: int = 10) -> Any:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()

# Μικρή τοπική cache για Dexscreener responses (TTL 60s)
_price_cache: Dict[str, tuple[float, float]] = {}
_CACHE_TTL = 60.0  # seconds

def _now() -> float:
    return time.time()

def _cache_get(key: str) -> Optional[float]:
    item = _price_cache.get(key)
    if not item:
        return None
    ts, val = item
    if _now() - ts > _CACHE_TTL:
        return None
    return val

def _cache_set(key: str, val: float) -> None:
    _price_cache[key] = (_now(), val)

def _normalize_asset_key(asset: str) -> str:
    return asset.strip().lower()

def _pick_best_pair(pairs: list[dict], prefer_chain: str = "cronos") -> Optional[dict]:
    if not pairs:
        return None
    # Προτίμηση σε ζεύγη πάνω στο ζητούμενο chain
    for p in pairs:
        if (p.get("chainId") or "").lower() == prefer_chain.lower():
            return p
    # Αλλιώς πάρε το πρώτο
    return pairs[0]

def _price_from_pairs(pairs: list[dict], prefer_chain: str = "cronos") -> Optional[float]:
    best = _pick_best_pair(pairs, prefer_chain=prefer_chain)
    if not best:
        return None
    price_str = best.get("priceUsd")
    try:
        return float(price_str) if price_str is not None else None
    except Exception:
        return None

def _price_by_token_address(chain: str, address: str) -> Optional[float]:
    # Dexscreener tokens endpoint
    url = f"https://api.dexscreener.com/latest/dex/tokens/{address}"
    data = _get_json(url, timeout=12)
    pairs = data.get("pairs") or []
    return _price_from_pairs(pairs, prefer_chain=chain)

def get_price_usd(asset: str, chain: str = "cronos") -> float:
    """
    Επιστρέφει USD price για asset:
      - Αν είναι address (0x...), ψάχνει Dexscreener /tokens/{address}
      - Αλλιώς χρησιμοποιεί/ενημερώνει HISTORY_LAST_PRICE
    Ποτέ δεν σηκώνει exception — αν δεν βρει τιμή, επιστρέφει 0.0.
    """
    if not asset:
        return 0.0

    key = _normalize_asset_key(asset)

    # 1) Cache
    cached = _cache_get(f"{chain}:{key}")
    if cached is not None:
        return cached

    price: Optional[float] = None

    # 2) Αν μοιάζει με address → Dexscreener
    if key.startswith("0x") and len(key) in (42, 66):
        try:
            price = _price_by_token_address(chain, key)
        except Exception:
            price = None

    # 3) Αν δεν βρήκε από address ή asset είναι σύμβολο → δοκίμασε HISTORY
    if price is None:
        # Προσπάθησε με σύμβολο/alias από HISTORY
        sym = asset.upper()
        if sym in HISTORY_LAST_PRICE:
            price = HISTORY_LAST_PRICE[sym]

    # 4) Τελικό fallback: 0.0 (δεν σπάμε ροή)
    if price is None:
        price = 0.0

    # 5) Ενημέρωσε caches
    _cache_set(f"{chain}:{key}", price)
    # Αν είναι "καθαρό" σύμβολο, κρατάμε και στο HISTORY_LAST_PRICE
    if not key.startswith("0x"):
        HISTORY_LAST_PRICE[asset.upper()] = price

    return price

def seed_price(symbol: str, price_usd: float) -> None:
    """Προαιρετικό helper για pre-seed τιμών σε HISTORY_LAST_PRICE."""
    if not symbol:
        return
    try:
        p = float(price_usd)
    except Exception:
        return
    HISTORY_LAST_PRICE[symbol.upper()] = p
