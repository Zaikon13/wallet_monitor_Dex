# -*- coding: utf-8 -*-
from __future__ import annotations
from decimal import Decimal
from typing import Any, Dict, Iterable, Optional, Tuple

from core.holdings import Position, get_wallet_snapshot

# Optional imports — adapters auto-detect available functions
try:
    from core import rpc as _rpc  # type: ignore
except Exception:
    _rpc = None  # shim fallback

try:
    from core import pricing as _pricing  # type: ignore
except Exception:
    _pricing = None  # shim fallback

# --- helpers -------------------------------------------------------------

def _to_dec(x: Any) -> Decimal:
    try:
        if isinstance(x, Decimal):
            return x
        return Decimal(str(x))
    except Exception:
        return Decimal(0)

# --- price getter --------------------------------------------------------

def price_getter(symbol: str, address: Optional[str], is_native: bool) -> Decimal:
    """
    Returns USD price per unit for (symbol,address,is_native).
    Uses core.pricing.get_price_usd(symbol) when available.
    """
    if _pricing and hasattr(_pricing, "get_price_usd"):
        try:
            p = _pricing.get_price_usd(symbol)  # type: ignore[attr-defined]
            return _to_dec(p) if p is not None else Decimal(0)
        except Exception:
            return Decimal(0)
    return Decimal(0)

# --- positions fetcher ---------------------------------------------------

def _fetch_native_cro() -> Optional[Position]:
    """
    Try multiple RPC shapes to obtain native CRO balance.
    Expected contracts: address=None, is_native=True.
    """
    if not _rpc:
        return None

    # Heuristics over likely function names/signatures:
    candidates = [
        "get_native_cro_balance",  # () or (address)
        "get_native_balance",      # (symbol='CRO') or ()
        "get_balance_cro",         # ()
    ]
    balance = None
    for name in candidates:
        fn = getattr(_rpc, name, None)
        if not fn:
            continue
        try:
            # Try calling with and without address; adapters should accept both
            try:
                balance = fn()  # type: ignore[misc]
            except TypeError:
                balance = fn(symbol="CRO")  # type: ignore[misc]
            break
        except Exception:
            continue

    if balance is None:
        return None

    amt = _to_dec(balance)
    if amt == 0:
        return None

    return Position(symbol="CRO", amount=amt, address=None, is_native=True, cost_usd=Decimal(0))


def _iter_erc20_positions() -> Iterable[Position]:
    """
    Try to fetch ERC20-like balances from RPC. Supports several common shapes:
      - rpc.get_erc20_balances() -> list[{'symbol','address','amount'}]
      - rpc.list_balances() -> list[...] with keys above
    Unknown keys are ignored safely.
    """
    if not _rpc:
        return []

    for name in ("get_erc20_balances", "list_balances"):
        fn = getattr(_rpc, name, None)
        if not fn:
            continue
        try:
            items = fn()  # type: ignore[misc]
        except Exception:
            continue
        if not items:
            continue

        out = []
        for it in items:
            try:
                sym = str((it.get("symbol") or "")).upper()
                addr = it.get("address")
                amt = _to_dec(it.get("amount"))
                if not sym or amt == 0:
                    continue
                # tCRO or any wrapped token should carry contract address and is_native=False
                out.append(Position(symbol=sym, amount=amt, address=addr, is_native=False, cost_usd=Decimal(0)))
            except Exception:
                continue
        if out:
            return out
    return []


def fetch_positions() -> Iterable[Position]:
    """
    Build Position lots from providers/RPC with CRO kept as native.
    Cost basis (cost_usd) left 0 here — merged unrealized PnL still works (value - cost).
    """
    out = []
    cro = _fetch_native_cro()
    if cro:
        out.append(cro)
    out.extend(list(_iter_erc20_positions()))
    return out

# --- public facade -------------------------------------------------------

def build_holdings_snapshot(base_ccy: str = "USD") -> Dict[str, Any]:
    """
    Returns holdings snapshot via core.holdings.get_wallet_snapshot()
    using the adapters above. This is the function callers should use.
    """
    return get_wallet_snapshot(fetch_positions, price_getter, base_ccy=base_ccy)
