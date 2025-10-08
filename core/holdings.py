# -*- coding: utf-8 -*-
"""
core/holdings.py — authoritative wallet snapshot for Telegram `/holdings`, `/show`, etc.

Notes
-----
- No network or schedule side-effects at import time.
- Depends on:
    * core.rpc.list_balances()                 -> iterable[{symbol, amount, address?}]
    * core.pricing.get_spot_usd(symbol, addr)  -> float | Decimal | None
  These are optional and guarded: on failure we degrade gracefully (price=0).
- CRO merge:
    * Any 'tCRO', 'TCRO', 'wcro receipt' is merged into 'CRO'.
- uPnL:
    * Best-effort attempt to fetch avg cost; if unavailable -> cost/uPnL set to 0.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---- Optional dependencies (guarded) -----------------------------------
try:
    from core import rpc  # type: ignore
except Exception:  # pragma: no cover
    rpc = None  # type: ignore

try:
    from core import pricing  # type: ignore
except Exception:  # pragma: no cover
    pricing = None  # type: ignore

# Cost-basis (optional)
try:
    from reports.ledger import get_avg_cost_usd  # type: ignore
except Exception:  # pragma: no cover
    def get_avg_cost_usd(symbol: str) -> Optional[Decimal]:
        return None

D = Decimal

CRO_ALIASES = {"TCRO", "tCRO", "tcro", "T-CRO", "t-cro", "WCRO-RECEIPT"}


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip()
    if s in CRO_ALIASES:
        return "CRO"
    # Normalize common wrappers
    if s.upper() in {"WCRO"}:
        return "CRO"
    return s or "?"


def _to_decimal(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return D(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return D("0")


@dataclass
class AssetSnap:
    symbol: str
    amount: Decimal
    price_usd: Decimal
    value_usd: Decimal
    cost_usd: Decimal
    u_pnl_usd: Decimal
    u_pnl_pct: Decimal

    def to_row(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "amount": str(self.amount.normalize()),
            "price_usd": str(self.price_usd.quantize(D("0.00000001"))),
            "value_usd": str(self.value_usd.quantize(D("0.01"))),
            "cost_usd": str(self.cost_usd.quantize(D("0.01"))),
            "u_pnl_usd": str(self.u_pnl_usd.quantize(D("0.01"))),
            "u_pnl_pct": str(self.u_pnl_pct.quantize(D("0.01"))),
        }


def _erc20_candidates() -> Sequence[tuple[str, str]]:
    """
    Return (symbol, contract) pairs to probe on Cronos.
    Extend from env CRO_TOKENS='SYMBOL:0xaddr,SYMBOL2:0xaddr2' if present.
    Includes a small built-in set as a safety net.
    """
    env = os.getenv("CRO_TOKENS", "").strip()
    pairs: list[tuple[str, str]] = []
    if env:
        for part in env.split(","):
            if ":" in part:
                s, a = part.strip().split(":", 1)
                if s and a:
                    pairs.append((s.strip(), a.strip()))
    # Minimal built-ins (safe/popular Cronos tokens — edit if needed)
    builtins = [
        ("VVS", "0x2D03bece6747ADC00E1a131BBA1469C15fD11e03"),
        ("TONIC", "0xEcbF7bA5a39d3b0B8E1406E1FF07C1a1e2D12dE2"),
        ("USDC", "0xc21223249CA28397B4B6541dfFaEcC539BfF0c59"),
        ("USDT", "0x66e428c3f67a68878562E79A0234c1F83c208770"),
        ("DAI", "0xf2001B145b43032AAF5Ee2884e456CCd805F677D"),
    ]
    # Append builtins only if not duplicated by env
    existing = {a.lower() for _, a in pairs}
    for s, a in builtins:
        if a.lower() not in existing:
            pairs.append((s, a))
    return pairs


# ---- Core snapshot ------------------------------------------------------
def _fetch_balances() -> Iterable[Dict[str, Any]]:
    """
    Strategy:
      1) Live rpc.list_balances()
      2) If empty: rpc.rescan() and retry
      3) If still empty: native CRO via Web3 (get_balance)
      4) If still empty: ERC20 balanceOf() scan for a small set of tokens
      5) If still empty: local cache files
    """
    balances: list[Dict[str, Any]] = []

    # 1) direct
    if rpc is not None and hasattr(rpc, "list_balances"):
        try:
            data = rpc.list_balances() or []
            if isinstance(data, list):
                balances = data
        except Exception:
            balances = []

    # 2) rescan
    if (not balances) and (rpc is not None) and hasattr(rpc, "rescan"):
        try:
            rpc.rescan()
        except Exception:
            pass
        try:
            data = rpc.list_balances() or []
            if isinstance(data, list):
                balances = data
        except Exception:
            balances = []

    # 3) native CRO
    if not balances:
        cro = _fallback_native_cro_balance()
        if cro is not None:
            balances = [cro]

    # 4) ERC20 fallback
    if not balances:
        erc20s = _fallback_erc20_balances()
        if erc20s:
            balances = erc20s

    # 5) cache
    if not balances:
        try:
            import json
            import pathlib

            for c in ("./.cache/balances.json", "./data/balances.json", "./.ledger/balances.json"):
                p = pathlib.Path(c)
                if p.exists() and p.is_file():
                    data = json.loads(p.read_text(encoding="utf-8")) or []
                    if isinstance(data, list) and data:
                        balances = data
                        break
        except Exception:
            pass

    return balances or []


def _fallback_native_cro_balance() -> Optional[Dict[str, Any]]:
    """
    Read native CRO balance directly from RPC when higher-level adapter returns empty.
    Requires:
      - CRONOS_RPC_URL
      - WALLET_ADDRESS (hex)
    Returns a balance row or None if not available.
    """
    rpc_url = os.getenv("CRONOS_RPC_URL") or os.getenv("CRONOSRPCURL")
    wallet = os.getenv("WALLET_ADDRESS") or os.getenv("WALLETADDRESS")
    if not rpc_url or not wallet:
        return None
    try:
        from web3 import Web3  # type: ignore

        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            return None
        wei = w3.eth.get_balance(Web3.to_checksum_address(wallet))
        cro = Decimal(wei) / Decimal(10**18)
        # Ignore dust-level noise; treat < 1e-12 as zero
        if cro.compare(Decimal("0.000000000001")) <= 0:
            return {"symbol": "CRO", "amount": Decimal("0"), "address": None}
        return {"symbol": "CRO", "amount": cro, "address": None}
    except Exception:
        return None


def _fallback_erc20_balances() -> list[Dict[str, Any]]:
    """
    Probe a small set of known ERC20 tokens on Cronos using balanceOf.
    Requires CRONOS_RPC_URL + WALLET_ADDRESS.
    """
    rpc_url = os.getenv("CRONOS_RPC_URL") or os.getenv("CRONOSRPCURL")
    wallet = os.getenv("WALLET_ADDRESS") or os.getenv("WALLETADDRESS")
    if not rpc_url or not wallet:
        return []
    try:
        from web3 import Web3  # type: ignore
        from eth_abi import abi  # type: ignore  # noqa: F401
    except Exception:
        return []
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            return []
        acct = Web3.to_checksum_address(wallet)
        sig = Web3.keccak(text="balanceOf(address)")[:4]  # function selector
        out: list[Dict[str, Any]] = []
        for symbol, contract in _erc20_candidates():
            try:
                addr = Web3.to_checksum_address(contract)
                data = sig + bytes.fromhex(acct[2:].rjust(64, "0"))
                raw = w3.eth.call({"to": addr, "data": "0x" + data.hex()})
                if raw and raw != b"\x00" * 32:
                    bal = int(raw.hex(), 16)
                    dec_sig = Web3.keccak(text="decimals()")[:4]
                    dec_raw = w3.eth.call({"to": addr, "data": "0x" + dec_sig.hex()})
                    decimals = int(dec_raw.hex(), 16) if dec_raw else 18
                    qty = Decimal(bal) / (Decimal(10) ** Decimal(decimals))
                    if qty > Decimal("0"):
                        out.append({"symbol": symbol, "amount": qty, "address": addr})
            except Exception:
                continue
        return out
    except Exception:
        return []


def _spot_usd(symbol: str, address: Optional[str]) -> Decimal:
    if pricing is None or not hasattr(pricing, "get_spot_usd"):
        return D("0")
    try:
        px = pricing.get_spot_usd(symbol=symbol, token_address=address)
        return _to_decimal(px)
    except Exception:
        return D("0")


def _avg_cost(symbol: str) -> Decimal:
    try:
        c = get_avg_cost_usd(symbol)  # type: ignore
        return _to_decimal(c)
    except Exception:
        return D("0")


def _merge_rows(rows: Iterable[Dict[str, Any]]) -> List[Tuple[str, Decimal, Optional[str]]]:
    """
    Merge raw balances by normalized symbol (CRO + tCRO -> CRO).
    Returns list of (symbol, total_amount, preferred_address).
    """
    agg: Dict[str, Tuple[Decimal, Optional[str]]] = {}
    for r in rows:
        sym = _norm_symbol(str(r.get("symbol") or r.get("token") or ""))
        amt = _to_decimal(r.get("amount") or r.get("qty") or r.get("balance"))
        addr = r.get("address") or r.get("token_address") or r.get("contract")
        if sym not in agg:
            agg[sym] = (amt, addr)
        else:
            cur_amt, cur_addr = agg[sym]
            agg[sym] = (cur_amt + amt, cur_addr or addr)
    # drop zero rows
    out: List[Tuple[str, Decimal, Optional[str]]] = [
        (s, a, ad) for s, (a, ad) in agg.items() if a != 0
    ]
    # sort by symbol for deterministic output
    out.sort(key=lambda t: t[0])
    return out


def get_wallet_snapshot(base_ccy: str = "USD", limit: int = 9999) -> Dict[str, Any]:
    """
    Public API used by telegram commands.

    Returns:
        {
          "assets": [ {symbol, amount, price_usd, value_usd, cost_usd, u_pnl_usd, u_pnl_pct}, ... ],
          "totals": { value_usd, cost_usd, u_pnl_usd, u_pnl_pct }
        }
    """
    raw = list(_fetch_balances())
    merged = _merge_rows(raw)

    snaps: List[AssetSnap] = []
    total_val = D("0")
    total_cost = D("0")

    for sym, amt, addr in merged:
        px = _spot_usd(sym, addr)
        val = (amt * px).quantize(D("0.00000001"))
        cost_per_unit = _avg_cost(sym)
        cost = (cost_per_unit * amt) if cost_per_unit else D("0")
        upnl = (val - cost)
        upct = (upnl / cost * 100) if cost > 0 else D("0")
        snaps.append(
            AssetSnap(
                symbol=sym,
                amount=amt,
                price_usd=px,
                value_usd=val,
                cost_usd=cost,
                u_pnl_usd=upnl,
                u_pnl_pct=upct,
            )
        )
        total_val += val
        total_cost += cost

    # sort by value desc
    snaps.sort(key=lambda s: s.value_usd, reverse=True)
    if limit and limit > 0:
        snaps = snaps[:limit]

    total_upnl = total_val - total_cost
    total_upct = (total_upnl / total_cost * 100) if total_cost > 0 else D("0")

    return {
        "assets": [s.to_row() for s in snaps],
        "totals": {
            "value_usd": str(total_val.quantize(D("0.01"))),
            "cost_usd": str(total_cost.quantize(D("0.01"))),
            "u_pnl_usd": str(total_upnl.quantize(D("0.01"))),
            "u_pnl_pct": str(total_upct.quantize(D("0.01"))),
        },
    }


# Back-compat alias some projects used
wallet_snapshot = get_wallet_snapshot
