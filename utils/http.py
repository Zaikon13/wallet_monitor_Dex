# -*- coding: utf-8 -*-
import time
from typing import Any, Dict, Optional
import requests

DEFAULT_HEADERS = {
    "User-Agent": "Cronos-DeFi-Sentinel/1.0 (+https://github.com/Zaikon13/wallet_monitor_Dex)"
}

def safe_get(url: str, params: Optional[Dict[str, Any]] = None,
             timeout: int = 10, retries: int = 1, backoff: float = 0.5):
    """
    Lightweight GET with retries.
    Returns requests.Response on success, else None.
    """
    params = params or {}
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=DEFAULT_HEADERS, timeout=timeout)
            r.raise_for_status()
            return r
        except Exception:
            if attempt >= retries:
                return None
            time.sleep(backoff * (2 ** attempt))
    return None

def safe_json(resp) -> Optional[Dict[str, Any]]:
    """
    Convert Response -> dict or return None.
    """
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return None
