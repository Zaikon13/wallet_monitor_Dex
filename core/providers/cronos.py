"""Cronos wallet provider using CronoScan (Etherscan-compatible) API.
- Fetches latest normal txs (native CRO) and ERC-20 token transfers
- Converts to generic Tx dicts understood by WalletMonitor
- Attempts simple SWAP detection by grouping tokentx legs per tx hash

Env vars used:
- ETHERSCAN_API (CronoScan API key)
- CRONOSCAN_API (optional override for API key)
- CRONOSSCAN_BASE (optional base URL, default https://api.cronoscan.com/api)

Notes:
- This is a lightweight poller: it fetches recent txs (descending) and returns up to `limit`
- Price in USD is optional; if `core.pricing.get_price_usd` is available, we try to enrich leg USD values.
"""
from __future__ import annotations
from typing import List, Dict, Any, Optional, Tuple
from decimal import Decimal
import os
import time
import requests

Tx = Dict[str, object]

BASE = os.getenv("CRONOSSCAN_BASE", "https://api.cronoscan.com/api")
API_KEY = os.getenv("CRONOSCAN_API") or os.getenv("ETHERSCAN_API")

# simple in-memory last seen to avoid returning already-processed txs every time
_last_seen_ts: float = 0.0
_last_seen_hashes: set[str] = set()


def _get(url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    params = dict(params)
    if API_KEY:
        params["apikey"] = API_KEY
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def _to_decimal(value: str, decimals: int) -> Decimal:
    q = Decimal(value)
    scale = Decimal(10) ** Decimal(decimals)
    return (q / scale).quantize(Decimal("0.00000001"))


def _fetch_normal(address: str, limit: int) -> List[Tx]:
    # Native CRO txs
    try:
        data = _get(BASE, {
            "module": "account",
            "action": "txlist",
            "address": address,
            "sort": "desc",
        })
        if data.get("status") == "0":
            return []
        items = data.get("result", [])[:limit]
    except Exception:
        return []

    out: List[Tx] = []
    for it in items:
        try:
            txhash = it.get("hash")
            frm = (it.get("from") or "").lower()
            to  = (it.get("to") or "").lower()
            ts  = int(it.get("timeStamp") or 0)
            val_wei = it.get("value") or "0"
            amt = _to_decimal(val_wei, 18)
            side = "IN" if to == address.lower() else ("OUT" if frm == address.lower() else None)
            if not side or amt == 0:
                continue
            out.append({
                "txid": txhash,
                "time": time.strftime("%H:%M:%S", time.localtime(ts)),
                "side": side,
                "asset": "CRO",
                "qty": amt,
                "price_usd": None,
                "usd": None,
            })
        except Exception:
            continue
    return out


def _fetch_erc20(address: str, limit: int) -> Tuple[List[Tx], Dict[str, List[Tx]]]:
    # ERC-20 transfers; returns both flat list and grouped by tx hash for swap detection
    try:
        data = _get(BASE, {
            "module": "account",
            "action": "tokentx",
            "address": address,
            "sort": "desc",
        })
        if data.get("status") == "0":
            return [], {}
        items = data.get("result", [])[:limit]
    except Exception:
        return [], {}

    legs_by_hash: Dict[str, List[Tx]] = {}
    flat: List[Tx] = []
    addr_l = address.lower()

    for it in items:
        try:
            txhash = it.get("hash")
            frm = (it.get("from") or "").lower()
            to  = (it.get("to") or "").lower()
            ts  = int(it.get("timeStamp") or 0)
            sym = (it.get("tokenSymbol") or "TKN").upper()
            dec = int(it.get("tokenDecimal") or 18)
            amt = _to_decimal(it.get("value") or "0", dec)

            side = "IN" if to == addr_l else ("OUT" if frm == addr_l else None)
            if not side or amt == 0:
                continue
            leg: Tx = {
                "txid": txhash,
                "time": time.strftime("%H:%M:%S", time.localtime(ts)),
                "side": side,
                "asset": sym,
                "qty": amt,
                "price_usd": None,
                "usd": None,
            }
            flat.append(leg)
            legs_by_hash.setdefault(txhash, []).append(leg)
        except Exception:
            continue

    return flat, legs_by_hash


def _try_enrich_usd(legs: List[Tx]) -> None:
    try:
        from core.pricing import get_price_usd
    except Exception:
        return
    for leg in legs:
        try:
            p = get_price_usd(leg.get("asset") or "")
            if p:
                leg["price_usd"] = Decimal(str(p))
                leg["usd"] = (leg["qty"] * leg["price_usd"]).quantize(Decimal("0.00000001"))
        except Exception:
            pass


def fetch_wallet_txs(address: str, *, limit: int = 25) -> List[Tx]:
    """Return recent wallet txs as generic Tx dicts. Performs simple swap grouping.
    Output Tx shapes:
      - IN/OUT single-asset transfer
      - SWAP with legs=[ {side IN/OUT, asset, qty, price_usd, usd}, ... ]
    Dedup is handled by WalletMonitor (txid set), so we can return recent window each time.
    """
    if not address:
        return []
    addr = address.strip()

    # fetch ERC-20 first to detect swap legs; then native CRO
    erc_flat, legs_by_hash = _fetch_erc20(addr, limit)
    normal = _fetch_normal(addr, limit)

    # Build swaps: txs with both an OUT and IN leg
    swaps: List[Tx] = []
    for h, legs in legs_by_hash.items():
        has_in = any((l.get("side") == "IN") for l in legs)
        has_out = any((l.get("side") == "OUT") for l in legs)
        if has_in and has_out:
            _try_enrich_usd(legs)
            swaps.append({
                "txid": h,
                "time": legs[0].get("time"),
                "side": "SWAP",
                "asset": "*",
                "qty": Decimal("0"),
                "legs": legs,
            })

    # Singles = normal CRO transfers + ERC20 transfers that weren't part of swap legs (e.g. pure deposit/withdraw)
    swap_hashes = {s.get("txid") for s in swaps}
    singles: List[Tx] = []

    def _not_swap(x: Tx) -> bool:
        return x.get("txid") not in swap_hashes

    singles.extend([x for x in erc_flat if _not_swap(x)])
    singles.extend([x for x in normal if _not_swap(x)])

    # Order by time desc within a simple window (WalletMonitor will dedup by txid anyway)
    def _ts_key(x: Tx):
        # we only have HH:MM:SS; for coarse ordering assume recent first as fetched
        return 0

    out = swaps + singles
    return out[:limit]
