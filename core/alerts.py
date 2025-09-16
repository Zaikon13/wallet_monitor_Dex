import os
import time
from typing import Optional, Dict

try:
    from utils.http import safe_get, safe_json
except ImportError:
    import requests
    def safe_get(url: str, params=None, timeout: int = 10, retries: int = 1, backoff: float = 1.5):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            return r if r.ok else None
        except Exception:
            return None
    def safe_json(response):
        if response is None:
            return None
        try:
            return response.json()
        except Exception:
            return None

try:
    from telegram.api import send_telegram
except ImportError:
    def send_telegram(text: str):
        print("[TELEGRAM]", text)

# Suppress duplicate alerts for 1 hour
_last_alert: Dict[str, float] = {}
_SUPPRESS_SEC = 3600

def _should_alert(key: str) -> bool:
    last = _last_alert.get(key, 0.0)
    if time.time() - last < _SUPPRESS_SEC:
        return False
    _last_alert[key] = time.time()
    return True

def check_pair_alert(token_address: str, chain: str = "cronos") -> Optional[str]:
    """
    Checks Dexscreener for a token and sends an alert if 24h price change exceeds thresholds.
    Trigger thresholds from env PUMP_ALERT_24H_PCT / DUMP_ALERT_24H_PCT (default +20% / -20%).
    """
    if not token_address or not token_address.lower().startswith("0x"):
        return None
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    try:
        data = safe_json(safe_get(url, timeout=12))
    except Exception as e:
        return f"Error fetching Dexscreener: {e}"
    if not data:
        return None
    pairs = data.get("pairs") or []
    if not pairs:
        return None
    # Prefer pair on specified chain (Cronos)
    pair = None
    for p in pairs:
        if str(p.get("chainId", "")).lower() == chain.lower():
            pair = p
            break
    if not pair:
        pair = pairs[0]
    # Determine 24h price change
    change = None
    if "priceChange24h" in pair:
        try:
            change = float(pair["priceChange24h"])
        except Exception:
            change = None
    elif pair.get("priceChange"):
        try:
            change = float((pair["priceChange"] or {}).get("h24", 0))
        except Exception:
            change = None
    if change is None:
        return None
    symbol = (pair.get("baseToken") or {}).get("symbol", token_address[:6])
    price_usd = pair.get("priceUsd", "?")
    # Thresholds (defaults 20/-20 if not set)
    try:
        pump_threshold = float(os.getenv("PUMP_ALERT_24H_PCT", "20"))
    except Exception:
        pump_threshold = 20.0
    try:
        dump_threshold = float(os.getenv("DUMP_ALERT_24H_PCT", "-20"))
    except Exception:
        dump_threshold = -20.0
    if change >= pump_threshold:
        key = f"{token_address}-pump"
        if _should_alert(key):
            msg = f"🚀 Pump alert: {symbol} +{change:.1f}% (24h) — ${price_usd}"
            send_telegram(msg)
            return msg
    elif change <= dump_threshold:
        key = f"{token_address}-dump"
        if _should_alert(key):
            msg = f"💀 Dump alert: {symbol} {change:.1f}% (24h) — ${price_usd}"
            send_telegram(msg)
            return msg
    return None
