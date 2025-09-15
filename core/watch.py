# core/watch.py
import os, json, time
from utils.http import safe_get, safe_json

DEX_BASE_SEARCH = "https://api.dexscreener.com/latest/dex/search"


def load_watchlist(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_watchlist(path: str, wl: list):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(wl, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def scan_watchlist(wl: list, min_liq=10000, min_vol24=5000):
    alerts = []
    for pair in wl or []:
        try:
            data = safe_json(safe_get(DEX_BASE_SEARCH, params={"q": pair}, timeout=12)) or {}
            for p in data.get("pairs") or []:
                if str(p.get("chainId", "")).lower() != "cronos":
                    continue
                liq = float((p.get("liquidity") or {}).get("usd") or 0)
                vol = float((p.get("volume") or {}).get("h24") or 0)
                if liq < min_liq or vol < min_vol24:
                    continue
                price = float(p.get("priceUsd") or 0)
                ch24 = float((p.get("priceChange") or {}).get("h24") or 0)
                alerts.append({
                    "pair": pair,
                    "token0": (p.get("baseToken") or {}).get("symbol"),
                    "token1": (p.get("quoteToken") or {}).get("symbol"),
                    "price": price,
                    "change": ch24,
                    "vol24h": vol,
                    "liq_usd": liq,
                    "url": f"https://dexscreener.com/cronos/{p.get('pairAddress')}",
                })
        except Exception:
            continue
    return alerts
