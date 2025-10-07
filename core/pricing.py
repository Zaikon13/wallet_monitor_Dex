# -*- coding: utf-8 -*-
"""
Pricing helpers with bounded caches.

Public API
----------
- get_price_usd(symbol: str) -> Decimal | None
- set_price_hint(symbol: str, price: Decimal | str | float | int) -> None  # optional seeding

Notes
-----
- Uses Dexscreener public API (best-effort). If network fails, returns last cached price (if not expired).
- Caches are **bounded** (LRU) and **TTL’d** to prevent memory creep in long-running processes.
- Symbol matching is heuristic (by `baseSymbol` or `symbol`), prioritizing USD stable quotes (USDC/USDT)
  and then WCRO pairs as fallback.

Env
---
None (uses generic public endpoints). If you have stricter needs, wire a provider key here.
"""
from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from utils.http import get_json


# ---------- General helpers ----------
def _map_from_env(key: str, env: Mapping[str, str] | None = None) -> Dict[str, str]:
    """Return symbol->value mapping from CSV env vars or KEY_* entries."""
    import os

    source = env or os.environ
    mapping: Dict[str, str] = {}

    raw = (source.get(key) or "").strip()
    if raw:
        for part in raw.split(","):
            if not part or "=" not in part:
                continue
            name, value = part.split("=", 1)
            name = name.strip().upper()
            value = value.strip()
            if name and value:
                mapping[name] = value

    prefix = f"{key}_"
    for env_key, value in source.items():
        if not env_key.startswith(prefix):
            continue
        name = env_key[len(prefix) :].strip().upper()
        val = (value or "").strip()
        if name and val:
            mapping[name] = val

    return mapping


# ---------- Cache settings ----------
_PRICE_TTL_SEC = 60            # how long a price is considered fresh
_PRICE_MAX_ITEMS = 512         # hard bound on symbol->price cache
_HINT_TTL_SEC = 6 * 3600       # hints last longer (they don't hit network)
_HINT_MAX_ITEMS = 2048

# ---------- Internal LRU caches ----------
# cache: symbol_upper -> (Decimal price, float ts)
_PRICE_CACHE: "OrderedDict[str, Tuple[Decimal, float]]" = OrderedDict()
# last-known price hints (e.g., seeded from elsewhere)
_PRICE_HINTS: "OrderedDict[str, Tuple[Decimal, float]]" = OrderedDict()

# prefer these quote symbols when selecting among pairs
_STABLES = {"USDC", "USDT"}
# acceptable alt quote on Cronos
_ALT_QUOTES = {"WCRO", "CRO"}


# ---------- LRU helpers ----------
def _now() -> float:
    return monotonic()


def _to_decimal(val: Any) -> Optional[Decimal]:
    try:
        if val is None:
            return None
        if isinstance(val, Decimal):
            return val
        return Decimal(str(val))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _lru_get(
    cache: "OrderedDict[str, Tuple[Decimal, float]]",
    key: str,
    ttl: int,
) -> Optional[Decimal]:
    key = key.upper()
    item = cache.get(key)
    if not item:
        return None
    price, ts = item
    if _now() - ts > ttl:
        # stale: drop and miss
        try:
            del cache[key]
        except KeyError:
            pass
        return None
    # refresh recency
    cache.move_to_end(key, last=True)
    return price


def _lru_put(
    cache: "OrderedDict[str, Tuple[Decimal, float]]",
    key: str,
    value: Decimal,
    max_items: int,
) -> None:
    key = key.upper()
    cache[key] = (value, _now())
    cache.move_to_end(key, last=True)
    # bound size
    while len(cache) > max_items:
        cache.popitem(last=False)


# ---------- Public seeding API ----------
def set_price_hint(symbol: str, price: Any) -> None:
    """Optionally seed a last-known price without doing network calls."""
    p = _to_decimal(price)
    if p is None:
        return
    _lru_put(_PRICE_HINTS, symbol, p, _HINT_MAX_ITEMS)


# ---------- Dexscreener fetch ----------
def _dexscreener_search(symbol: str) -> Dict[str, Any] | None:
    """
    Search pairs by symbol. Returns raw JSON dict or None.
    Endpoint returns up to ~20 matches; we'll filter client-side.
    """
    q = symbol.strip()
    if not q:
        return None
    url = "https://api.dexscreener.com/latest/dex/search"
    return get_json(url, params={"q": q}, retries=2, timeout=10)


def _select_best_pair(symbol: str, data: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[str]]:
    """
    Choose the most relevant pair for the symbol:
    - prefer baseSymbol == symbol (case-insensitive)
    - prefer quotes in _STABLES, then in _ALT_QUOTES
    - break ties by higher liquidity (fdv/liq fields may vary; use 'liquidity.usd' when available)
    Returns (price_usd, quoteSymbol) or (None, None).
    """
    symbol_u = symbol.upper()
    pairs: Iterable[Dict[str, Any]] = data.get("pairs") or []

    def _liq_usd(p: Dict[str, Any]) -> Decimal:
        liq = (p.get("liquidity") or {}).get("usd")
        d = _to_decimal(liq)
        return d or Decimal("0")

    # rank pairs
    ranked: list[Tuple[int, Decimal, Dict[str, Any]]] = []
    for p in pairs:
        base_sym = (p.get("baseToken") or {}).get("symbol") or p.get("baseSymbol") or ""
        quote_sym = (p.get("quoteToken") or {}).get("symbol") or p.get("quoteSymbol") or ""
        if not base_sym:
            continue
        score = 0
        if base_sym.upper() == symbol_u:
            score += 10
        if quote_sym.upper() in _STABLES:
            score += 5
        elif quote_sym.upper() in _ALT_QUOTES:
            score += 2
        ranked.append((score, _liq_usd(p), p))

    if not ranked:
        return None, None

    ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
    best = ranked[0][2]

    # extract price in USD
    # Dexscreener often gives 'priceUsd' as string
    price_usd = _to_decimal(best.get("priceUsd"))
    if price_usd is None:
        # sometimes priceNative + a quote price could be present; ignore for now (keep simple)
        return None, None

    quote_sym = (best.get("quoteToken") or {}).get("symbol") or best.get("quoteSymbol")
    quote_sym = (quote_sym or "").upper()
    return price_usd, quote_sym


# ---------- Public pricing API ----------
def get_price_usd(symbol: str) -> Optional[Decimal]:
    """
    Return current USD price for `symbol` as Decimal (or None if unknown).
    - Checks bounded TTL cache first
    - Falls back to Dexscreener search
    - Seeds/uses hints when network is down
    """
    if not symbol:
        return None
    sym = symbol.upper()

    # 1) fresh cache
    p = _lru_get(_PRICE_CACHE, sym, _PRICE_TTL_SEC)
    if p is not None:
        return p

    # 2) try network
    data = _dexscreener_search(sym)
    if data:
        price_usd, _quote = _select_best_pair(sym, data)
        if price_usd is not None:
            _lru_put(_PRICE_CACHE, sym, price_usd, _PRICE_MAX_ITEMS)
            # also refresh hint
            _lru_put(_PRICE_HINTS, sym, price_usd, _HINT_MAX_ITEMS)
            return price_usd

    # 3) fallback: last known hint (may be stale)
    p = _lru_get(_PRICE_HINTS, sym, _HINT_TTL_SEC)
    return p
