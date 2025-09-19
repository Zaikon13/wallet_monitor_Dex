# core/holdings.py
"""
Wallet holdings snapshot helpers (robust to varying RPC helpers).
- Does NOT depend on a non-existent get_wallet_balances.
- Uses core.rpc.get_native_balance and, if available, ERC-20 helpers.
- Returns clean dict: {symbol: {"amount": Decimal, "usd_value": Decimal}}
- Keeps CRO and tCRO distinct (no merging).
"""

from __future__ import annotations
import logging
from decimal import Decimal
from typing import Dict, Iterable, Tuple, Any

# Lazy/defensive imports so we don't explode if functions differ between versions
from core import rpc as rpc_mod
from core.pricing import get_price_usd


# ---------- Helpers ----------

def _safe_decimal(x: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def _add_entry(snapshot: Dict[str, Dict[str, Decimal]], symbol: str, amount: Decimal) -> None:
    if not symbol:
        return
    # Keep tCRO separate end-to-end
    sym = symbol.strip()
    if sym not in snapshot:
        snapshot[sym] = {"amount": Decimal("0"), "usd_value": Decimal("0")}
    snapshot[sym]["amount"] += amount


def _iter_erc20_pairs(obj: Any) -> Iterable[Tuple[str, Decimal]]:
    """
    Accepts many shapes:
      - dict { "JASMY": "123.45", "HBAR": 10 }
      - list[ { "symbol": "JASMY", "amount": "123.45" }, ... ]
      - list[ (symbol, amount), ... ]
    Yields (symbol, Decimal(amount)).
    """
    if obj is None:
        return []
    # dict form
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, _safe_decimal(v)
        return
    # list/iterable form
    if isinstance(obj, (list, tuple, set)):
        for item in obj:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                sym, amt = item
                yield str(sym), _safe_decimal(amt)
            elif isinstance(item, dict):
                sym = item.get("symbol") or item.get("ticker") or item.get("sym") or item.get("token") or item.get("name")
                amt = item.get("amount") or item.get("balance") or item.get("qty") or item.get("value")
                if sym is not None and amt is not None:
                    yield str(sym), _safe_decimal(amt)
        return
    # Fallback: nothing
    return []


def _maybe_get_erc20_balances(wallet_address: str):
    """
    Try multiple candidate functions that may exist in core.rpc.
    Return None if none are available.
    """
    candidates = [
        "get_erc20_balances",
        "list_erc20_balances",
        "get_token_balances",
        "list_token_balances",
    ]
    for name in candidates:
        if hasattr(rpc_mod, name):
            try:
                return getattr(rpc_mod, name)(wallet_address)
            except Exception as e:
                logging.warning(f"ERC20 balance fetch via {name} failed: {e}")
                return None
    return None


def _price_for(symbol: str) -> Decimal:
    """
    Get USD price for a symbol with sensible fallbacks.
    We try:
      - direct symbol
      - WCRO when asking for CRO (common canonical pair)
    """
    try:
        p = get_price_usd(symbol)
        return _safe_decimal(p)
    except Exception:
        pass

    # CRO often priced via WCRO
    if symbol.upper() == "CRO":
        try:
            p = get_price_usd("WCRO")
            return _safe_decimal(p)
        except Exception:
            return Decimal("0")

    return Decimal("0")


# ---------- Public API ----------

def get_wallet_snapshot(wallet_address: str, include_usd: bool = True) -> Dict[str, Dict[str, Decimal]]:
    """
    Build a snapshot from whatever RPC helpers exist.
    Returns: {symbol: {"amount": Decimal, "usd_value": Decimal}}
    """
    snapshot: Dict[str, Dict[str, Decimal]] = {}

    # 1) Native CRO
    if hasattr(rpc_mod, "get_native_balance"):
        try:
            cro_amt = _safe_decimal(rpc_mod.get_native_balance(wallet_address))
            _add_entry(snapshot, "CRO", cro_amt)
        except Exception as e:
            logging.exception(f"Failed to fetch native CRO balance: {e}")
    else:
        logging.warning("core.rpc.get_native_balance not found")

    # 2) ERC-20 balances (if any helper exists)
    erc20 = _maybe_get_erc20_balances(wallet_address)
    if erc20 is not None:
        try:
            for sym, amt in _iter_erc20_pairs(erc20):
                # Do NOT merge tCRO into CRO — keep distinct
                _add_entry(snapshot, str(sym), _safe_decimal(amt))
        except Exception as e:
            logging.exception(f"Failed to process ERC-20 balances: {e}")

    # 3) Price to USD
    if include_usd:
        for sym, data in snapshot.items():
            price = _price_for(sym)
            data["usd_value"] = (data.get("amount", Decimal("0")) * price).quantize(Decimal("0.00000001"))

    return snapshot


def format_snapshot_lines(snapshot: Dict[str, Dict[str, Decimal]]) -> list[str]:
    """
    Convert snapshot dict into list of formatted text lines (stable ordering).
    CRO first, then alphabetical.
    """
    ordered = sorted(snapshot.items(), key=lambda kv: (kv[0] != "CRO", kv[0].upper()))
    lines = []
    for sym, data in ordered:
        amt = data.get("amount", Decimal("0"))
        usd = data.get("usd_value", Decimal("0"))
        # 8 decimals for amount, 2 for USD
        lines.append(f"{sym}: {amt:.8f} ≈ ${usd:.2f}")
    return lines
