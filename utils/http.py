# -*- coding: utf-8 -*-
import time, random, logging
import requests
from typing import Optional

log = logging.getLogger("http")

_session = None
def _get_session():
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": "CronosDefiSentinel/1.0"})
    return _session

# Simple circuit breaker
_CB_FAILS = 0
_CB_OPEN_UNTIL = 0.0
_CB_THRESHOLD = 6
_CB_COOLDOWN = 20.0  # seconds

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

def safe_get(url: str, params: Optional[dict]=None, timeout: int=15, retries: int=0):
    """
    GET with jittered exponential backoff + circuit breaker. Never raises requests exceptions.
    """
    if _cb_open():
        time.sleep(0.2)
        return None
    sess = _get_session()
    attempt = 0
    delay = 0.6
    while True:
        try:
            r = sess.get(url, params=params, timeout=timeout)
            if r.status_code >= 500:
                raise requests.RequestException(f"HTTP {r.status_code}")
            _cb_note_success()
            return r
        except Exception as e:
            log.debug("safe_get error (%s): %s", url, e)
            _cb_note_failure()
            if attempt >= retries:
                return None
            attempt += 1
            time.sleep(delay + random.uniform(0, delay*0.2))
            delay = min(delay * 2, 5.0)

def safe_json(resp):
    try:
        if resp is None: return None
        return resp.json()
    except Exception:
        return None
