# core/watch.py
from __future__ import annotations
import os, json, time, logging
from typing import List, Dict, Any
from utils.http import safe_get, safe_json

log = logging.getLogger("core.watch")

DATA_DIR = os.getenv("DATA_DIR", "/app/data")
STATE_PATH = os.path.join(DATA_DIR, "watch_state.json")

WATCH_COOLDOWN_MIN = int(os.getenv("WATCH_COOLDOWN_MIN", "30"))
WATCH_MIN_ABS_CHANGE_PCT = float(os.getenv("WATCH_MIN_ABS_CHANGE_PCT", "8"))
WATCH_MIN_LIQ_USD = float(os.getenv("WATCH_MIN_LIQ_USD", "30000"))
WATCH_MIN_VOL24_USD = float(os.getenv("WATCH_MIN_VOL24_USD", "5000"))

DEX_BASE = "https://api.dexscreener.com/latest/dex"
URL_PAIRS = f"{DEX_BASE}/pairs"
URL_SEARCH = f"{DEX_BASE}/search"

def load_watchlist(path: str) -> list[str]:
    """Load watchlist (list of strings like 'cronos/<pair>' or free text queries)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            wl = json.load(f)
        if isinstance(wl, list):
            return [str(x).strip() for x in wl if str(x).strip()]
    except Exception:
        pass
    # default empty
    return []

def save_watchlist(path: str, items: list[str]):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log.warning("save_watchlist error: %s", e)

def _read_state() -> dict:
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _write_state(d: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.warning("write_state error: %s", e)

def _cooldown_ok(key: str) -> bool:
    st = _read_state()
    last = float(st.get("last", {}).get(key, 0.0))
    now = time.time()
    if now - last >= WATCH_COOLDOWN_MIN * 60:
        st.setdefault("last", {})[key] = now
        _write_state(st)
        return True
    return False

def _pick_best(pairs: list[dict]) -> dict | None:
    """Pick the pair with highest liquidity on Cronos."""
    best, best_liq = None, -1.0
    for p in pairs or []:
        try:
            if str(p.get("chainId", "")).lower() != "cronos":
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if liq > best_liq:
                best_liq, best = liq, p
        except Exception:
            continue
    return best

def _fetch_by_slug(s: str) -> dict | None:
    # s: "cronos/<pairAddress>"
    url = f"{URL_PAIRS}/{s}"
    r = safe_get(url, timeout=12)
    d = safe_json(r) or {}
    if isinstance(d.get("pair"), dict):
        return d["pair"]
    if isinstance(d.get("pairs"), list) and d["pairs"]:
        return d["pairs"][0]
    return None

def _search(q: str) -> dict | None:
    r = safe_get(URL_SEARCH, params={"q": q}, timeout=12)
    d = safe_json(r) or {}
    return _pick_best(d.get("pairs") or [])

def _pair_to_alert(pair: dict) -> dict | None:
    try:
        price = float(pair.get("priceUsd") or 0)
        ch = pair.get("priceChange") or {}
        ch24 = float(ch.get("h24")) if "h24" in ch else None
        vol24 = float((pair.get("volume") or {}).get("h24") or 0)
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        if (liq < WATCH_MIN_LIQ_USD) or (vol24 < WATCH_MIN_VOL24_USD):
            return None
        # require abs change threshold on any window (h1/h4/h6/h24)
        abs_ok = False
        for k in ("h1", "h4", "h6", "h24"):
            if k in ch:
                try:
                    if abs(float(ch[k])) >= WATCH_MIN_ABS_CHANGE_PCT:
                        abs_ok = True
                        break
                except Exception:
                    pass
        if not abs_ok:
            return None
        bt = pair.get("baseToken") or {}
        qt = pair.get("quoteToken") or {}
        return {
            "pair": f"{bt.get('symbol','?')}/{qt.get('symbol','?')}",
            "token0": bt.get("symbol"), "token1": qt.get("symbol"),
            "price": price, "change": ch24, "vol24h": vol24, "liq_usd": liq,
            "url": f"https://dexscreener.com/cronos/{pair.get('pairAddress')}",
        }
    except Exception:
        return None

def scan_watchlist(items: list[str]) -> list[dict]:
    """
    Επιστρέφει λίστα από alerts dicts.
    Watchlist items examples:
      - "cronos/0xPAIRADDRESS..."
      - "mery wcro"  (ελεύθερο search)
    """
    out: list[dict] = []
    if not items:
        return out
    for it in items:
        s = (it or "").strip().lower()
        try:
            if not s:
                continue
            if s.startswith("cronos/"):
                pair = _fetch_by_slug(s)
            else:
                pair = _search(s)
            if not pair:
                continue
            alert = _pair_to_alert(pair)
            if not alert:
                continue
            key = pair.get("pairAddress") or alert["pair"]
            if _cooldown_ok(f"watch:{key}"):
                out.append(alert)
        except Exception as e:
            log.debug("scan_watchlist item error %s: %s", s, e)
            continue
    return out
