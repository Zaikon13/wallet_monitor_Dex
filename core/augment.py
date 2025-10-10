# core/augment.py — merge base snapshot with discovered tokens (non-invasive, Decimal-safe)
from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Dict, Any, List, Set, Optional

from core.discovery import discover_tokens_for_wallet
from core.pricing import get_spot_usd

def _to_dec(x: Any) -> Optional[Decimal]:
    if isinstance(x, Decimal):
        return x
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None

def _index_existing_symbols(snapshot: Dict[str, Any]) -> Set[str]:
    existing: Set[str] = set()
    for a in snapshot.get("assets", []) or []:
        sym = str(a.get("symbol", "")).upper()
        if sym:
            existing.add(sym)
    return existing

def augment_with_discovered_tokens(snapshot: Dict[str, Any], wallet_address: str) -> Dict[str, Any]:
    """
    Non-invasive: κρατάμε το base snapshot, προσθέτουμε tokens με balance>0
    που ανακαλύπτουμε από discovery (χωρίς να πειράξουμε core/holdings.py).
    """
    if not wallet_address:
        return snapshot

    base_assets: List[Dict[str, Any]] = list(snapshot.get("assets", []) or [])
    assets: List[Dict[str, Any]] = list(base_assets)
    existing_syms = _index_existing_symbols(snapshot)

    discovered = discover_tokens_for_wallet(wallet_address)
    added_any = False

    for t in discovered:
        sym = str(t.get("symbol", "")).upper() or (t.get("address", "")[:6]).upper()
        if sym in existing_syms:
            continue  # skip duplicates
        amt_dec = _to_dec(t.get("amount"))
        if amt_dec is None or amt_dec <= 0:
            continue

        addr = t.get("address")
        price = get_spot_usd(sym, token_address=addr)  # μπορεί να είναι None
        price_dec = _to_dec(price) or Decimal("0")
        val_dec = (amt_dec * price_dec) if price_dec is not None else Decimal("0")

        assets.append({
            "symbol": sym,
            "amount": amt_dec,           # Decimal
            "price_usd": price_dec,      # Decimal
            "value_usd": val_dec,        # Decimal
            "address": addr,
            "decimals": t.get("decimals"),
        })
        existing_syms.add(sym)
        added_any = True

    if not added_any:
        return snapshot

    # Recompute simple totals (value only; cost/uPnL κρατάμε/συγχωνεύουμε προσεκτικά)
    # Μαζεύουμε ΟΛΕΣ τις τιμές (ακόμα κι από base assets) ως Decimal
    total_value = Decimal("0")
    for a in assets:
        v = _to_dec(a.get("value_usd")) or Decimal("0")
        total_value += v

    totals = dict(snapshot.get("totals") or {})
    totals["value_usd"] = total_value

    out = dict(snapshot)
    out["assets"] = assets
    out["totals"] = totals
    return out
