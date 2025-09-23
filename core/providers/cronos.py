# core/providers/cronos.py
from __future__ import annotations
from typing import List, Dict, Any, Optional
from decimal import Decimal
import time

from core.providers.etherscan_like import account_txlist, account_tokentx

Tx = Dict[str, object]

def _D(x) -> Decimal:
    try:
        return Decimal(str(x if x is not None else 0))
    except Exception:
        return Decimal("0")

def _to_amount(value_any, decimals_any) -> Decimal:
    """Μετατρέπει raw value (hex ή decimal str/int) σε Decimal ποσότητα με decimals."""
    v = str(value_any or "0")
    try:
        base = int(v, 16) if v.startswith(("0x", "0X")) else int(v)
    except Exception:
        try:
            base = int(Decimal(v))
        except Exception:
            base = 0
    try:
        dec = int(decimals_any if decimals_any is not None else 18)
    except Exception:
        dec = 18
    if dec < 0:
        dec = 0
    scale = Decimal(10) ** Decimal(dec)
    return (Decimal(base) / scale).quantize(Decimal("0.00000001"))

def _fmt_hms(epoch_sec: int) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(int(epoch_sec)))
    except Exception:
        return ""

def _side_for(addr_lower: str, to_: str, frm_: str) -> Optional[str]:
    to_l  = (to_  or "").lower()
    frm_l = (frm_ or "").lower()
    if to_l == addr_lower:  return "IN"
    if frm_l == addr_lower: return "OUT"
    return None

# --------- Optional pricing (safe import) ----------
def _get_price_usd(sym: str) -> Optional[Decimal]:
    try:
        from core.pricing import get_price_usd  # lazy import
    except Exception:
        return None
    try:
        p = get_price_usd((sym or "").upper())
        return Decimal(str(p)) if p is not None else None
    except Exception:
        return None

def _enrich_leg(l: Dict[str, Any]) -> None:
    """Mutates leg in-place, sets price_usd & usd if available."""
    sym = (l.get("asset") or "").upper()
    qty = _D(l.get("qty"))
    px = _get_price_usd(sym)
    if px is not None:
        l["price_usd"] = px
        l["usd"] = (qty * px).quantize(Decimal("0.00000001"))
    else:
        # keep None for price_usd/usd if not available
        l.setdefault("price_usd", None)
        l.setdefault("usd", None)

def _enrich_single(tx: Dict[str, Any]) -> None:
    """Mutates single transfer dict, sets price_usd & usd if available."""
    sym = (tx.get("asset") or "").upper()
    qty = _D(tx.get("qty"))
    px = _get_price_usd(sym)
    if px is not None:
        tx["price_usd"] = px
        tx["usd"] = (qty * px).quantize(Decimal("0.00000001"))
    else:
        tx.setdefault("price_usd", None)
        tx.setdefault("usd", None)

# --------- Public API ----------
def fetch_wallet_txs(address: str, *, limit: int = 50, enrich_usd: bool = True) -> List[Tx]:
    """
    Επιστρέφει πρόσφατες συναλλαγές wallet σε γενική μορφή:
      - SWAP: {"txid","time","side":"SWAP","asset":"*","qty":"0",
               "legs":[{side,asset,qty,price_usd,usd},...]}
      - Single transfer: {"txid","time","side","asset","qty","price_usd","usd"}

    Δε χρησιμοποιεί άμεσα requests — μιλάει με CronoScan μέσω
    core.providers.etherscan_like.{account_txlist,account_tokentx}.
    """
    if not address:
        return []
    addr_l = address.lower()

    # 1) Πάρε normal txs & token μεταφορές
    txs  = (account_txlist(address)  or {}).get("result") or []
    toks = (account_tokentx(address) or {}).get("result") or []

    # 2) Ομαδοποίησε ERC-20 ανά hash
    legs_by_hash: Dict[str, List[Dict[str, Any]]] = {}
    for tr in toks:
        h = tr.get("hash")
        if not h:
            continue
        legs_by_hash.setdefault(h, []).append(tr)

    out: List[Tx] = []

    # 3) Διέτρεξε τα normal txs
    for tx in txs:
        h = tx.get("hash")
        if not h:
            continue
        ts  = int(tx.get("timeStamp") or 0)
        when = _fmt_hms(ts)

        xfers = legs_by_hash.get(h, [])
        if xfers:
            legs: List[Dict[str, Any]] = []
            for tr in xfers:
                sym = (tr.get("tokenSymbol") or "?").upper()
                amt = _to_amount(tr.get("value"), tr.get("tokenDecimal"))
                if amt == 0:
                    continue
                side = _side_for(addr_l, tr.get("to",""), tr.get("from",""))
                if not side:
                    continue
                leg = {
                    "side": side,
                    "asset": sym,
                    "qty": str(amt),
                    "price_usd": None,
                    "usd": None,
                }
                if enrich_usd:
                    _enrich_leg(leg)
                legs.append(leg)

            if not legs:
                continue

            has_in  = any(l.get("side") == "IN"  for l in legs)
            has_out = any(l.get("side") == "OUT" for l in legs)

            if has_in and has_out:
                # SWAP
                out.append({
                    "txid": h,
                    "time": when,
                    "side": "SWAP",
                    "asset": "*",
                    "qty": "0",
                    "legs": legs,
                })
                continue

            # Διαφορετικά: κάθε leg ως μεμονωμένη κίνηση
            for l in legs:
                item = {
                    "txid": h,
                    "time": when,
                    "side": l["side"],
                    "asset": l["asset"],
                    "qty": l["qty"],
                    "price_usd": l.get("price_usd"),
                    "usd": l.get("usd"),
                }
                if enrich_usd and item.get("price_usd") is None:
                    _enrich_single(item)
                out.append(item)

        else:
            # Native CRO transfer
            val = _to_amount(tx.get("value"), 18)
            if val == 0:
                continue
            side = _side_for(addr_l, tx.get("to",""), tx.get("from",""))
            if not side:
                continue
            item = {
                "txid": h,
                "time": when,
                "side": side,
                "asset": "CRO",
                "qty": str(val),
                "price_usd": None,
                "usd": None,
            }
            if enrich_usd:
                _enrich_single(item)
            out.append(item)

    # 4) Παράθυρο πρόσφατων
    return out[:limit]
