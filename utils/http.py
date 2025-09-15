# utils/http.py
import requests, time

_DEF_HEADERS = {"User-Agent": "wallet-monitor/1.0"}

def safe_get(url: str, params: dict | None = None, timeout: int = 15, retries: int = 2):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, headers=_DEF_HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r
            # basic 429/backoff
            if r.status_code == 429:
                time.sleep(1.0 * (i + 1))
        except Exception as e:
            last = e
        time.sleep(0.4 * (i + 1))
    return None

def safe_json(resp):
    try:
        if resp is None: return None
        return resp.json()
    except Exception:
        return None
