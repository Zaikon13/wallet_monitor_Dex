# -*- coding: utf-8 -*-
"""HTTP utilities with safe defaults and JSON helpers."""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

import requests

DEFAULT_HEADERS = {
    "User-Agent": "Cronos-DeFi-Sentinel/1.0 (+https://github.com/Zaikon13/wallet_monitor_Dex)"
}


def safe_get(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
    retries: int = 1,
    backoff: float = 0.5,
):
    """Lightweight GET with retries. Returns ``requests.Response`` on success, else ``None``."""
    params = params or {}
    for attempt in range(retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=DEFAULT_HEADERS,
                timeout=timeout,
            )
            response.raise_for_status()
            return response
        except Exception:
            if attempt >= retries:
                return None
            time.sleep(backoff * (2 ** attempt))
    return None


def safe_json(resp) -> Optional[Dict[str, Any]]:
    """Convert ``requests.Response`` -> ``dict`` or return ``None`` if parsing fails."""
    if resp is None:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def get_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 10,
    retries: int = 1,
    backoff: float = 0.5,
):
    """Convenience wrapper combining :func:`safe_get` and :func:`safe_json`."""
    response = safe_get(
        url,
        params=params,
        timeout=timeout,
        retries=retries,
        backoff=backoff,
    )
    return safe_json(response)
