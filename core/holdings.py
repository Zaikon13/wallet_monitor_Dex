from __future__ import annotations

"""Wallet holdings snapshot utilities."""

import os
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Optional

from core.providers.etherscan_like import (
    account_balance,
    account_tokentx,
    token_balance,
)
from core.pricing import get_price_usd


def _map_from_env(key: str) -> Dict[str, str]:
    """Parse an env var like "SYMA=0x1234,SYMB=0xabcd" into a dict."""
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
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _normalize_symbol(symbol: str) -> str:
    raw = (symbol or "").strip()
    if raw.lower() == "tcro":
        return "tCRO"
    if not raw:
        return "?"
    return raw.upper()


def _sanitize_snapshot(raw: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    sanitized: Dict[str, Dict[str, Any]] = {}
    for symbol, info in (raw or {}).items():
        key = _normalize_symbol(symbol)
        data = dict(info or {})
        data.setdefault("symbol", key)
        sanitized[key] = data
    return sanitized


def get_wallet_snapshot(address: str | None = None) -> Dict[str, Dict[str, Optional[str]]]:
    """Build a snapshot of wallet holdings (CRO native + configured ERC-20 tokens)."""
    address = address or os.getenv("WALLET_ADDRESS", "")
    if not address:
        return {}

    snap: Dict[str, Dict[str, Optional[str]]] = {}

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
        pass

    addr_map = _map_from_env("TOKENS_ADDRS")
    dec_map = _map_from_env("TOKENS_DECIMALS")
    for sym, contract in addr_map.items():
        try:
            raw = token_balance(contract, address).get("result")
            if raw is None:
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
            snap.setdefault(sym, {"qty": "0", "price_usd": None, "usd": None})

    try:
        toks = (account_tokentx(address) or {}).get("result") or []
        for t in toks[-50:]:
            sym = (t.get("tokenSymbol") or "?").strip()
            norm = _normalize_symbol(sym)
            if norm and norm not in snap:
                snap[norm] = {"qty": "?", "price_usd": None, "usd": None}
    except Exception:
        pass

    return snap


def holdings_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return a sanitized snapshot dict suitable for formatting."""
    try:
        raw = get_wallet_snapshot()
    except Exception:
        raw = {}
    sanitized = _sanitize_snapshot(raw)
    if "TCRO" in sanitized and "tCRO" not in sanitized:
        sanitized["tCRO"] = sanitized.pop("TCRO")
    return sanitized


def _format_money(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    try:
        sign = "-" if value < 0 else ""
        magnitude = abs(value)
        if magnitude == 0:
            return "$0.00"
        if magnitude >= 1:
            return f"{sign}${magnitude:,.2f}"
        return f"{sign}${magnitude:.6f}"
    except Exception:
        return "n/a"


def _format_qty(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    try:
        if value == 0:
            return "0"
        if abs(value) >= 1:
            return f"{value:,.4f}"
        return f"{value:.6f}"
    except Exception:
        return "n/a"


def _format_delta(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    try:
        if value == 0:
            return "$0.00"
        prefix = "+" if value > 0 else ""
        magnitude = abs(value)
        if magnitude >= 1:
            return f"{prefix}${magnitude:,.2f}"
        return f"{prefix}${magnitude:.6f}"
    except Exception:
        return "n/a"


def _usd_value(info: Dict[str, Any]) -> Optional[Decimal]:
    return _to_decimal(info.get("usd") or info.get("value_usd"))


def _price_value(info: Dict[str, Any]) -> Optional[Decimal]:
    return _to_decimal(info.get("price_usd") or info.get("price"))


def _delta_value(info: Dict[str, Any]) -> Optional[Decimal]:
    keys = ("pnl_usd", "delta_usd", "pnl", "change_usd", "unrealized_usd")
    for key in keys:
        val = _to_decimal(info.get(key))
        if val is not None:
            return val
    return None


def _ordered_symbols(snapshot: Dict[str, Dict[str, Any]]) -> Iterable[str]:
    keys = list(snapshot.keys())
    ordered: list[str] = []
    for special in ("CRO", "tCRO"):
        if special in keys:
            ordered.append(special)
            keys.remove(special)

    keys.sort(
        key=lambda sym: (
            _usd_value(snapshot.get(sym, {})) or Decimal("0"),
            sym,
        ),
        reverse=True,
    )
    ordered.extend(keys)
    return ordered


def holdings_text(snapshot: Dict[str, Dict[str, Any]] | None = None) -> str:
    """Format holdings with CRO-first ordering and safe fallbacks."""
    data = snapshot
    if data is None:
        try:
            data = holdings_snapshot()
        except Exception:
            data = {}

    if not data:
        return "No holdings data."

    lines = ["Holdings:"]
    total_usd = Decimal("0")
    for symbol in _ordered_symbols(data):
        info = data.get(symbol) or {}
        qty = _format_qty(_to_decimal(info.get("qty") or info.get("amount")))
        usd_val = _usd_value(info)
        price_val = _price_value(info)
        delta_val = _delta_value(info)
        total_usd += usd_val or Decimal("0")
        lines.append(
            " - {sym:<6} {qty} @ {price} → USD {usd} (Δ {delta})".format(
                sym=symbol,
                qty=qty,
                price=_format_money(price_val),
                usd=_format_money(usd_val),
                delta=_format_delta(delta_val),
            )
        )

    lines.append("")
    lines.append(f"Total ≈ {_format_money(total_usd)}")
    return "\n".join(lines)


def format_snapshot_lines(snapshot: Dict[str, Dict[str, Optional[str]]]) -> str:
    """Compatibility wrapper that proxies to holdings_text."""
    return holdings_text(snapshot)
