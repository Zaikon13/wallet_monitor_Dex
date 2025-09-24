# utils/http.py
try:
    import requests  # type: ignore
except Exception:  # fallback χωρίς requests
    requests = None

def get_json(url, params=None, timeout=25):
    if requests is not None:
        r = requests.get(url, params=params or {}, timeout=timeout)
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"status_code": r.status_code, "text": r.text}

    # Fallback με standard library
    import json, urllib.parse
    from urllib.request import urlopen, Request
    q = urllib.parse.urlencode(params or {})
    full = url + ("?" + q if q else "")
    req = Request(full, headers={"User-Agent": "wallet-monitor/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = resp.read()
        try:
            return json.loads(data.decode("utf-8", errors="ignore"))
        except Exception:
            # best-effort συμβατότητα με το παλιό shape
            return {"status_code": getattr(resp, "status", 200), "text": data.decode("utf-8", errors="ignore")}
