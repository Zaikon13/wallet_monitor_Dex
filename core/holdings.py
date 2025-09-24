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
    """
    Parse ENV like:  TOKENS_ADDRS="USDT=0x...,JASMY=0x..."  → {USDT: 0x..., JASMY: 0x...}
                     TOKENS_DECIMALS="USDT=6,JASMY=18"      → {USDT: 6,     JASMY: 18}
    """
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
    Snapshot using direct Cronos RPC (no etherscan needed).
    - Always returns CRO row (even if price is 0).
    - Tokens resolved from TOKENS_ADDRS (+ TOKENS_DECIMALS).
    Schema is compatible with formatters & day_report.
    """
    addr = (address or os.getenv("WALLET_ADDRESS", "")).strip()
    if not addr:
        return {"CRO": _mk_row(D("0"), D("0"))}

    snap: Dict[str, Dict[str, Any]] = {}

    # --- CRO native ---
    try:
        cro_amt = _dec(get_native_balance(addr))
        cro_px  = _dec(get_price_usd("CRO") or 0)
        snap["CRO"] = _mk_row(cro_amt, cro_px)
    except Exception:
        snap["CRO"] = _mk_row(D("0"), D("0"))

    # --- ERC-20 tokens from ENV ---
    addrs = _parse_map("TOKENS_ADDRS")
    decs  = _parse_map("TOKENS_DECIMALS")  # string values, default 18

    for sym, contract in (addrs or {}).items():
        if not contract: 
            continue
        try:
            raw = erc20_balance_of(contract, addr)  # int (wei)
            decimals = int(decs.get(sym, "18"))
            qty = D(raw) / (D(10) ** decimals)
            px  = _dec(get_price_usd(sym) or 0)
            snap[sym] = _mk_row(qty, px)
        except Exception:
            # still show symbol with zero to avoid "Empty snapshot"
            snap.setdefault(sym, _mk_row(D("0"), D("0")))

    # Guarantee non-empty
    if not snap:
        snap = {"CRO": _mk_row(D("0"), D("0"))}

    return snap
