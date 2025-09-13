import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_tls = threading.local()

def _get_session():
    s = getattr(_tls, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(
            total=3, connect=3, read=3, backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST")
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _tls.session = s
    return s

def safe_get(url, params=None, timeout=15, retries=0):
    # retries handled by session adapter; 'retries' param kept for API-compat
    return _get_session().get(url, params=params, timeout=timeout)

def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return None
