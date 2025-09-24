from __future__ import annotations
from typing import List, Dict
from decimal import Decimal
from core.providers.etherscan_like import account_txlist, account_tokentx

def _D(x): return Decimal(str(x or 0))

def fetch_wallet_txs(address: str) -> List[Dict[str, object]]:
    txs = (account_txlist(address) or {}).get("result") or []
    toks = (account_tokentx(address) or {}).get("result") or []
    by_hash = {}
    for t in toks:
        by_hash.setdefault(t.get("hash"), []).append(t)
    out = []
    for tx in txs:
        h=tx.get("hash")
        xfers = by_hash.get(h, [])
        if xfers:
            legs = []
            for tr in xfers:
                amt = _D(tr.get("value")) / (Decimal(10) ** int(tr.get("tokenDecimal") or 18))
                sym = (tr.get("tokenSymbol") or "?").upper()
                side = "IN" if (tr.get("to","") or "").lower()==address.lower() else "OUT"
                legs.append({"side":side, "asset":sym, "qty": str(amt), "price_usd": None, "usd": None})
            if any(l["side"]=="IN" for l in legs) and any(l["side"]=="OUT" for l in legs):
                out.append({"txid":h, "time": int(tx.get("timeStamp") or 0), "side":"SWAP", "legs":legs})
                continue
            for l in legs:
                out.append({"txid":h, "time": int(tx.get("timeStamp") or 0), "side":l["side"], "asset":l["asset"], "qty": l["qty"], "price_usd": None, "usd": None})
        else:
            val = _D(tx.get("value")) / Decimal(10) ** 18
            side = "IN" if (tx.get("to","") or "").lower()==address.lower() else "OUT"
            out.append({"txid":h, "time": int(tx.get("timeStamp") or 0), "side":side, "asset":"CRO", "qty": str(val), "price_usd": None, "usd": None})
    return out
