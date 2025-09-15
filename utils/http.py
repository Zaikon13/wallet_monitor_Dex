# utils/http.py
import time, logging, requests

__all__ = ["safe_get", "safe_json"]

log = logging.getLogger("utils.http")
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "DeFi-Sentinel/1.0"})

_last_req_ts = 0.0
REQS_PER_SEC = 5
MIN_GAP = 1.0 / REQS_PER_SEC


def safe_get(url, params=None, timeout=12, retries=3, backoff=1.5):
    """HTTP GET με retry/backoff & rate-limit."""
    global _last_req_ts
    for i in range(retries):
        gap = time.time() - _last_req_ts
        if gap < MIN_GAP:
            time.sleep(MIN_GAP - gap)
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            _last_req_ts = time.time()
            if r.status_code == 200:
                return r
            if r.status_code in (404, 429, 502, 503):
                time.sleep(backoff * (i + 1))
                continue
            return r
        except Exception as e:
            log.debug("safe_get error %s try %s", e, i + 1)
            time.sleep(backoff * (i + 1))
    return None


def safe_json(r):
    if r is None:
        return None
    if not getattr(r, "ok", False):
        return None
    try:
        return r.json()
    except Exception:
        return None
