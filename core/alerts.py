# core/alerts.py
# Simple price-move alerts (24h pump/dump) using Dexscreener

from __future__ import annotations
import time
from typing import Dict, Optional

try:
    from utils.http import safe_get, safe_json  # type: ignore
except Exception:
    safe_get = None
    safe_json = None

import requests

def get_json(url: str, timeout: int = 10):
    if safe_get:
        r = safe_get(url, timeout=timeout)
        return safe_json(r) if r is not None else None
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()

try:
    from telegram.api import send_telegram  # type: ignore
except Exception:
    def send_telegram(text: str, **kwargs):
        print("[TELEGRAM disabled]", text)
        return False, 0, "disabled"

_last_alert: Dict[str, float] = {}
_SUPPRESS_SEC = 3600  # 1 ώρα

def _now() -> float:
    return time.time()

def _should_alert(key: str) -> bool:
    last = _last_alert.get(key, 0.0)
    if _now() - last < _SUPPRESS_SEC:
        return False
    _last_alert[key] = _now()
    return True

def check_pair_alert(token_address: str, chain: str = "cronos") -> Optional[str]:
    """
    Ελέγχει Dexscreener για το token και στέλνει alert αν υπάρχει pump/dump.
    Trigger: priceChange.h24 >= +20% ή <= -20%.
    """
    if not token_address or not token_address.startswith("0x"):
        return None

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        data = get_json(url, timeout=12) or {}
    except Exception as e:
        return f"Error fetching Dexscreener: {e}"

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    pair = None
    for p in pairs:
        ch = p.get("priceChange") or {}
        if "h24" in ch:
            pair = p
            break
    if not pair:
        return None

    try:
        change = float((pair.get("priceChange") or {}).get("h24"))
    except Exception:
        return None

    symbol = (pair.get("baseToken") or {}).get("symbol") or token_address[:6]
    price_usd = pair.get("priceUsd", "?")

    if change >= 20.0:
        key = f"{token_address}-pump"
        if _should_alert(key):
            msg = f"🚀 Pump alert: {symbol} +{change:.1f}% (24h) — ${price_usd}"
            send_telegram(msg)
            return msg
    elif change <= -20.0:
        key = f"{token_address}-dump"
        if _should_alert(key):
            msg = f"💀 Dump alert: {symbol} {change:.1f}% (24h) — ${price_usd}"
            send_telegram(msg)
            return msg

    return None
