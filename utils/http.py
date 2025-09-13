# -*- coding: utf-8 -*-
"""
HTTP helpers with jittered exponential backoff, simple circuit breaker
and safe JSON parsing. All callers should import `safe_get` and `safe_json`.
"""
import time
import random
import logging
from typing import Optional

import requests

log = logging.getLogger("http")

_session = None

def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": "CronosDefiSentinel/1.0"})
    return _session

# ---------------- Circuit Breaker (very simple) ----------------
_CB_FAILS = 0
_CB_OPEN_UNTIL = 0.0
_CB_THRESHOLD = 6         # after 6 consecutive failures, open breaker
_CB_COOLDOWN = 20.0       # seconds to keep breaker open


def _cb_open() -> bool:
    return time.time() < _CB_OPEN_UNTIL


def _cb_note_failure():
    global _CB_FAILS, _CB_OPEN_UNTIL
    _CB_FAILS += 1
    if _CB_FAILS >= _CB_THRESHOLD:
        _CB_OPEN_UNTIL = time.time() + _CB_COOLDOWN
        log.warning("HTTP circuit breaker OPEN for %.1fs", _CB_COOLDOWN)


def _cb_note_success():
    global _CB_FAILS, _CB_OPEN_UNTIL
    _CB_FAILS = 0
    _CB_OPEN_UNTIL = 0.0


# ---------------- Public helpers ----------------

def safe_get(url: str, params: Optional[dict] = None, timeout: int = 15, retries: int = 0):
    """
    Perform a GET with jittered exponential backoff + circuit breaker.
    Never raises requests exceptions; returns `requests.Response` or `None`.

    Args:
        url: Target URL
        params: Query parameters
        timeout: Per-attempt timeout (seconds)
        retries: Number of retries (total attempts = retries + 1)
    """
    if _cb_open():
        # short sleep to avoid tight spin while breaker open
        time.sleep(0.2)
        return None

    sess = _get_session()
    attempt = 0
    delay = 0.6
    while True:
        try:
            r = sess.get(url, params=params, timeout=timeout)
            if r.status_code >= 500:
                # treat as retriable
                raise requests.RequestException(f"HTTP {r.status_code}")
            _cb_note_success()
            return r
        except Exception as e:
            log.debug("safe_get error (%s): %s", url, e)
            _cb_note_failure()
            if attempt >= retries:
                return None
            attempt += 1
            # exponential backoff with jitter
            time.sleep(delay + random.uniform(0, delay * 0.2))
            delay = min(delay * 2, 5.0)


def safe_json(resp):
    """Return resp.json() or None if parsing fails/resp is None."""
    try:
        if resp is None:
            return None
        return resp.json()
    except Exception:
        return None
