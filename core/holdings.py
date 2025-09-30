# -*- coding: utf-8 -*-
"""
Wallet holdings snapshot utilities.

Exports:
- get_wallet_snapshot(address: str | None = None) -> dict
- format_snapshot_lines(snapshot: dict) -> str
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Dict, Optional

from core.providers.etherscan_like import (
    account_balance,
    token_balance,
    account_tokentx,
)
from core.pricing import get_price_usd


def _map_from_env(key: str) -> Dict[str, str]:
    """
    Parse env var like "SYMA=0x1234,SYMB=0xabcd" into dict { "SYMA": "0x1234", ... }.
    Keys are upper-cased; values are stripped.
    """
    s = os.getenv(key, "").strip()
    if not s:
        return {}
    out: Dict[str, str] = {}
    for part in s.split(","):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip().upper()] = v.strip()
    return out


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def get_wallet_snapshot(address: str | None = None) -> Dict[str, Dict[str, Optional[str]]]:
    """
    Build a snapshot of wallet holdings (CRO native + configured ERC-20 tokens).

    Env support:
      - WALLET_ADDRESS: default address if `address` is None
      - TOKENS_ADDRS: comma-separated symbol=contract (e.g. "USDC=0x...,DAI=0x...")
      - TOKENS_DECIMALS: comma-separated symbol=decimals (e.g. "USDC=6,DAI=18")
    Returns a mapping:
      {
        "CRO": {"qty": "123.45", "price_usd": "0.05", "usd": "6.17"},
        "USDC": {"qty": "10", "price_usd": "1.0", "usd": "10.0"},
        ...
      }
    All numbers are serialized as strings for safe JSON transport.
    """
    address = address or os.getenv("WALLET_ADDRESS", "")
    if not address:
        return {}

    snap: Dict[str, Dict[str, Optional[str]]] = {}

    # 1) CRO native balance (wei → CRO)
    try:
        bal = account_balance(address).get("result")
        if bal is not None:
            cro = Decimal(str(bal)) / (Decimal(10) ** 18)
            px = get_price_usd("CRO")
            usd = (cro * px).quantize(Decimal("0.0001")) if px is not None else None
            snap["CRO"] = {
                "qty": str(cro.normalize()),
                "price_usd": (str(px) if px is not None else None),
                "usd": (str(usd) if usd is not None else None),
            }
    except Exception:
        # best-effort: ignore CRO if provider fails
        pass

    # 2) Tokens via TOKENS_ADDRS (+ optional TOKENS_DECIMALS)
    addr_map = _map_from_env("TOKENS_ADDRS")
    dec_map = _map_from_env("TOKENS_DECIMALS")
    for sym, contract in addr_map.items():
        try:
            raw = token_balance(contract, address).get("result")
            if raw is None:
                # include the symbol with zero qty if balance missing
                snap.setdefault(sym, {"qty": "0", "price_usd": None, "usd": None})
                continue

            decimals = int(dec_map.get(sym, "18"))
            qty = Decimal(str(raw)) / (Decimal(10) ** decimals)
            px = get_price_usd(sym)
            usd = (qty * px).quantize(Decimal("0.0001")) if px is not None else None

            snap[sym] = {
                "qty": str(qty.normalize()),
                "price_usd": (str(px) if px is not None else None),
                "usd": (str(usd) if usd is not None else None),
            }
        except Exception:
            # ensure symbol exists even on failure
            snap.setdefault(sym, {"qty": "0", "price_usd": None, "usd": None})

    # 3) (Optional) Discover recent token symbols from tokentx for visibility (no balances)
    try:
        toks = (account_tokentx(address) or {}).get("result") or []
        for t in toks[-50:]:
            sym = (t.get("tokenSymbol") or "?").upper()
            if sym and sym not in snap:
                snap[sym] = {"qty": "?", "price_usd": None, "usd": None}
    except Exception:
        pass

    return snap


def format_snapshot_lines(snapshot: Dict[str, Dict[str, Optional[str]]]) -> str:
    """
    Pretty-print snapshot for Telegram/console.
    Order by estimated USD value desc, then symbol.
    Falls back gracefully if values are missing.

    Example line:
      - CRO   123.4567 ≈ $6.17
    """
    if not snapshot:
        return "No holdings."

    def _usd(row: Dict[str, Optional[str]]) -> Decimal:
        val = row.get("usd")
        d = _to_decimal(val)
        return d if d is not None else Decimal("0")

    # sort by USD desc, then symbol
    items = sorted(snapshot.items(), key=lambda kv: (_usd(kv[1]), kv[0]), reverse=True)

    lines = []
    for sym, info in items:
        qty_str = info.get("qty") or "0"
        usd_val = _to_decimal(info.get("usd"))
        if usd_val is None:
            # unknown valuation
            lines.append(f" - {sym:<6} {qty_str}")
        else:
            # format qty (short) and usd (2dp)
            try:
                qd = Decimal(qty_str)
                if qd == 0:
                    qfmt = "0"
                elif abs(qd) >= 1:
                    qfmt = f"{qd:,.4f}"
                else:
                    qfmt = f"{qd:.6f}"
            except Exception:
                qfmt = qty_str
            lines.append(f" - {sym:<6} {qfmt} ≈ ${usd_val:,.2f}")

    total_usd = sum((_to_decimal(v.get("usd")) or Decimal("0")) for v in snapshot.values())
    lines.append("")
    lines.append(f"Total ≈ ${total_usd:,.2f}")

    return "\n".join(lines)
