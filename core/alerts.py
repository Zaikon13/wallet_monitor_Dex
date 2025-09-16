# core/alerts.py
# Simple price-move alerts (24h pump/dump) using Dexscreener

from __future__ import annotations
import os
import time
from typing import Dict, Any, Optional

try:
    from utils.http import get_json  # type: ignore
except Exception:
    import requests

    def get_json(url: str, timeout: int = 10):
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.json()

try:
    from telegram.api import send_telegram  # type: ignore
except Exception:
    def send_telegram(text: str, **kwargs):
        print("[TELEGRAM disabled]", text)
        return False, 0, "disabled"

# Cache για να μην spam-άρει το ίδιο alert συνέχεια
_last_alert: Dict[str, float] = {}
_SUPPRESS_SEC = 3600  # 1 ώρα

def _now() -> float:
    return time.time()

def _should_alert(key: str) -> bool:
    last = _last_alert.get(key, 0)
    if _now() - last < _SUPPRESS_SEC:
        return False
    _last_alert[key] = _now()
    return True

def check_pair_alert(token_address: str, chain: str = "cronos") -> Optional[str]:
    """
    Ελέγχει Dexscreener για το token και στέλνει alert αν υπάρχει pump/dump.
    Trigger: priceChange24h >= +20% ή <= -20%.
    """
    if not token_address or not token_address.startswith("0x"):
        return None

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        data = get_json(url, timeout=12)
    except Exception as e:
        return f"Error fetching Dexscreener: {e}"

    pairs = data.get("pairs") or []
    if not pairs:
        return None

    # Πάρε το πρώτο ζευγάρι που έχει priceChange24h
    pair = None
    for p in pairs:
        if "priceChange24h" in p:
            pair = p
            break
    if not pair:
        return None

    try:
        change = float(pair["priceChange24h"])
    except Exception:
        return None

    symbol = pair.get("baseToken", {}).get("symbol", token_address[:6])
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
