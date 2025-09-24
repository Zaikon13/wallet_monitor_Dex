from __future__ import annotations
from typing import Dict, List, Any
from decimal import Decimal
from core.providers.etherscan_like import account_txlist, account_tokentx

def _D(x): return Decimal(str(x or 0))

def safe_json(data: Any) -> Dict[str, object]:
    return data if isinstance(data, dict) else {}


def _coerce_tx_list(data: Any) -> List[Dict[str, object]]:
    resp = safe_json(data) or {}
    txs = resp.get("result") or resp.get("txs") or []
    if not isinstance(txs, list):
        return []
    out: List[Dict[str, object]] = []
    for item in txs:
        if isinstance(item, dict):
            out.append(item)
    return out

def _int(value) -> int:
    try:
        if value is None:
            return 0
        return int(str(value))
    except (TypeError, ValueError):
        return 0

def fetch_wallet_txs(address: str) -> List[Dict[str, object]]:
    txs = _coerce_tx_list(account_txlist(address))
    toks = _coerce_tx_list(account_tokentx(address))

    by_hash: Dict[str, List[Dict[str, object]]] = {}
    for t in toks:
        by_hash.setdefault(t.get("hash"), []).append(t)

    out: List[Dict[str, object]] = []
    addr_lower = address.lower()
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        h = tx.get("hash")
        xfers = by_hash.get(h, [])
        timestamp = _int(tx.get("timeStamp"))
        if xfers:
            legs: List[Dict[str, object]] = []
            for tr in xfers:
                try:
                    decimals = int(tr.get("tokenDecimal") or 18)
                except (TypeError, ValueError):
                    decimals = 18
                if decimals < 0:
                    decimals = 0
                amt = _D(tr.get("value")) / (Decimal(10) ** decimals)
                sym = (tr.get("tokenSymbol") or "?").upper()
                to_addr = (tr.get("to") or "").lower()
                side = "IN" if to_addr == addr_lower else "OUT"
                legs.append({
                    "side": side,
                    "asset": sym,
                    "qty": str(amt),
                    "price_usd": None,
                    "usd": None,
                })
            if any(l.get("side") == "IN" for l in legs) and any(l.get("side") == "OUT" for l in legs):
                out.append({"txid": h, "time": timestamp, "side": "SWAP", "legs": legs})
                continue
            for l in legs:
                out.append({
                    "txid": h,
                    "time": timestamp,
                    "side": l.get("side"),
                    "asset": l.get("asset"),
                    "qty": l.get("qty"),
                    "price_usd": None,
                    "usd": None,
                })
        else:
            val = _D(tx.get("value")) / (Decimal(10) ** 18)
            to_addr = (tx.get("to") or "").lower()
            side = "IN" if to_addr == addr_lower else "OUT"
            out.append({
                "txid": h,
                "time": timestamp,
                "side": side,
                "asset": "CRO",
                "qty": str(val),
                "price_usd": None,
                "usd": None,
            })
    return [entry for entry in out if isinstance(entry, dict)]
