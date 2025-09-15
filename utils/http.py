# utils/http.py
from __future__ import annotations
import time
import logging
import requests
from typing import Any, Optional

__all__ = ["safe_get", "safe_json"]

log = logging.getLogger("utils.http")

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "CronosSentinel/1.0"})

_last_req_ts = 0.0
REQS_PER_SEC = 5
MIN_GAP = 1.0 / REQS_PER_SEC


def safe_json(r: Optional[requests.Response]) -> Any:
    if r is None:
        return None
    try:
        return r.json()
    except Exception:
        return None


def safe_get(url: str, params: dict | None = None, timeout: int = 15, retries: int = 3, backoff: float = 1.5) -> Optional[requests.Response]:
    global _last_req_ts
    for i in range(retries + 1):
        gap = time.time() - _last_req_ts
        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            _last_req_ts = time.time()
            if r.status_code == 200:
                return r
            if r.status_code in (404, 429, 500, 502, 503):
                # backoff and retry
                time.sleep(backoff * (i + 1))
                continue
            return r
        except Exception as e:
            if i == retries:
                log.debug("safe_get failed: %s", e)
                return None
            time.sleep(backoff * (i + 1))
    return None
