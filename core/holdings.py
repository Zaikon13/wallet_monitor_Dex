import os
from decimal import Decimal
from core.providers.etherscan_like import account_balance, token_balance, account_tokentx
from core.pricing import get_price_usd

def _map_from_env(key: str) -> dict:
    s = os.getenv(key, "").strip()
    if not s:
        return {}
    out = {}
    for part in s.split(","):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip().upper()] = v.strip()
    return out

def get_wallet_snapshot(address: str | None = None):
    address = address or os.getenv("WALLET_ADDRESS", "")
    if not address:
        return {}
    snap: dict[str, dict] = {}

    # 1) CRO native balance (wei→CRO)
    try:
        bal = account_balance(address).get("result")
        if bal is not None:
            cro = Decimal(str(bal)) / (Decimal(10) ** 18)
            px = get_price_usd("CRO")
            usd = (cro * px).quantize(Decimal("0.0001")) if px is not None else None
            snap["CRO"] = {
                "qty": str(cro.normalize()),
                "price_usd": (str(px) if px is not None else None),
                "usd": (str(usd) if usd is not None else None),
            }
    except Exception:
        pass

    # 2) Tokens via TOKENS_ADDRS (+ optional TOKENS_DECIMALS)
    addr_map = _map_from_env("TOKENS_ADDRS")
    dec_map = _map_from_env("TOKENS_DECIMALS")
    for sym, contract in addr_map.items():
        try:
            raw = token_balance(contract, address).get("result")
            if raw is None:
                # include the symbol with zero qty
                snap.setdefault(sym, {"qty": "0", "price_usd": None, "usd": None})
                continue
            decimals = int(dec_map.get(sym, "18"))
            qty = Decimal(str(raw)) / (Decimal(10) ** decimals)
            px = get_price_usd(sym)
            usd = (qty * px).quantize(Decimal("0.0001")) if px is not None else None
            snap[sym] = {
                "qty": str(qty.normalize()),
                "price_usd": (str(px) if px is not None else None),
                "usd": (str(usd) if usd is not None else None),
            }
        except Exception:
            snap.setdefault(sym, {"qty": "0", "price_usd": None, "usd": None})

    # 3) (Optional) Discover recent token symbols from tokentx
    try:
        toks = (account_tokentx(address) or {}).get("result") or []
        for t in toks[-50:]:
            sym = (t.get("tokenSymbol") or "?").upper()
            if sym and sym not in snap:
                snap[sym] = {"qty": "?", "price_usd": None, "usd": None}
    except Exception:
        pass

    return snap
