import requests

def get_json(url, params=None, timeout=25):
    r = requests.get(url, params=params or {}, timeout=timeout)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text}
