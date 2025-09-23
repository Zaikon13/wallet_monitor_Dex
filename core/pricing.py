import os
from decimal import Decimal
from utils.http import get_json

def _map_from_env(key: str) -> dict:
    s = os.getenv(key, "").strip()
    if not s: return {}
    out = {}
    for part in s.split(","):
        if not part: continue
        if "=" not in part: continue
        k,v = part.split("=",1)
        out[k.strip().upper()] = v.strip()
    return out

def _dexscreener_price_by_address(addr: str) -> Decimal | None:
    if not addr: return None
    url = f"https://api.dexscreener.com/latest/dex/tokens/{addr}"
    data = get_json(url)
    pairs = data.get("pairs") or []
    if not pairs: return None
    # choose the highest liquidity pair
    best = sorted(pairs, key=lambda p: float(p.get("liquidity",{}).get("usd",0.0)), reverse=True)[0]
    px = best.get("priceUsd")
    try: return Decimal(str(px))
    except Exception: return None

def get_price_usd(symbol: str) -> Decimal | None:
    symbol = (symbol or "").upper()
    if not symbol: return None
    # CRO via WCRO
    if symbol == "CRO":
        wcro = os.getenv("CRO_WRAPPED_ADDR","").strip()
        if wcro:
            return _dexscreener_price_by_address(wcro)
        return None
    # explicit mapping
    addr_map = _map_from_env("TOKENS_ADDRS")
    if symbol in addr_map:
        return _dexscreener_price_by_address(addr_map[symbol])
    return None
