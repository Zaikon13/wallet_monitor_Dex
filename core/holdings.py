# core/holdings.py
from __future__ import annotations
import os
from decimal import Decimal as D
from typing import Dict, Any

from core.providers.cronos import get_native_balance, erc20_balance_of
from core.pricing import get_price_usd

def _dec(x) -> D:
    try: return D(str(x))
    except Exception: return D("0")

def _parse_map(env_key: str) -> dict[str, str]:
    raw = (os.getenv(env_key) or "").strip()
    if not raw: return {}
    out: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part: continue
        k, v = part.split("=", 1)
        k = (k or "").strip().upper()
        v = (v or "").strip()
        if k: out[k] = v
    return out

def _mk_row(qty: D, px: D | None) -> Dict[str, Any]:
    px = _dec(px or 0)
    usd = qty * px if px != 0 else D("0")
    return {
        "amount": qty,      # legacy shape
        "usd_value": usd,
        "qty": qty,         # new shape
        "price_usd": px,
        "usd": usd,
    }

def get_wallet_snapshot(address: str | None = None) -> Dict[str, Dict[str, Any]]:
    """
    Snapshot via Cronos RPC only (no etherscan).
    - Always includes CRO row (never empty dict).
    - Tokens from TOKENS_ADDRS (+ TOKENS_DECIMALS).
    """
    addr = (address or os.getenv("WALLET_ADDRESS", "")).strip()
    snap: Dict[str, Dict[str, Any]] = {}

    # CRO always present (even if 0 / no price)
    try:
        cro_amt = _dec(get_native_balance(addr)) if addr else D("0")
    except Exception:
        cro_amt = D("0")
    try:
        cro_px = _dec(get_price_usd("CRO") or 0)
    except Exception:
        cro_px = D("0")
    snap["CRO"] = _mk_row(cro_amt, cro_px)

    # Tokens from ENV
    addrs = _parse_map("TOKENS_ADDRS")
    decs  = _parse_map("TOKENS_DECIMALS")  # defaults to 18 if missing
    for sym, contract in (addrs or {}).items():
        if not contract: 
            continue
        try:
            raw = erc20_balance_of(contract, addr) if addr else 0
            decimals = int(decs.get(sym, "18"))
            qty = D(raw) / (D(10) ** decimals)
            px  = _dec(get_price_usd(sym) or 0)
            snap[sym] = _mk_row(qty, px)
        except Exception:
            # still expose the symbol to avoid "Empty snapshot"
            snap.setdefault(sym, _mk_row(D("0"), D("0")))

    # Never return empty dict
    if not snap:
        snap = {"CRO": _mk_row(D("0"), D("0"))}
    return snap
