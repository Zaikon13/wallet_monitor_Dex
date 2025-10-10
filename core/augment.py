# core/augment.py — merge base snapshot with discovered tokens (non-invasive)
from __future__ import annotations
from decimal import Decimal
from typing import Dict, Any, List, Set

from core.discovery import discover_tokens_for_wallet
from core.pricing import get_spot_usd

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

    assets: List[Dict[str, Any]] = list(snapshot.get("assets", []) or [])
    existing_syms = _index_existing_symbols(snapshot)

    discovered = discover_tokens_for_wallet(wallet_address)
    added_any = False

    for t in discovered:
        sym = str(t.get("symbol", "")).upper() or t.get("address", "")[:6]
        if sym in existing_syms:
            continue  # skip duplicates
        amt = t.get("amount")
        if amt is None:
            continue
        try:
            dec_amt = Decimal(str(amt))
        except Exception:
            continue
        if dec_amt <= 0:
            continue

        addr = t.get("address")
        price = get_spot_usd(sym, token_address=addr)
        val = price * dec_amt if price is not None else Decimal("0")

        assets.append({
            "symbol": sym,
            "amount": dec_amt,
            "price_usd": price or Decimal("0"),
            "value_usd": val,
            "address": addr,
            "decimals": t.get("decimals"),
        })
        existing_syms.add(sym)
        added_any = True

    if not added_any:
        return snapshot

    # Recompute simple totals (value only; cost/uPnL keep from base if exist)
    total_value = sum((a.get("value_usd") or 0) for a in assets)
    totals = dict(snapshot.get("totals") or {})
    totals["value_usd"] = total_value

    out = dict(snapshot)
    out["assets"] = assets
    out["totals"] = totals
    return out
