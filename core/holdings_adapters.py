# -*- coding: utf-8 -*-
"""Adapters for holdings snapshot -> UI/monitor friendly shapes."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List

from core.holdings import holdings_snapshot

__all__ = ["build_holdings_snapshot"]

_DECIMAL_ZERO = Decimal("0")
_DECIMAL_ONE_HUNDRED = Decimal("100")


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if value in (None, "", "n/a", "NA"):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _quantize(value: Decimal | None, places: int) -> Decimal | None:
    if value is None:
        return None
    try:
        quant = Decimal("1").scaleb(-places)
        return value.quantize(quant)
    except Exception:
        return value


def _decimal_to_str(value: Decimal | None, *, places: int | None = None, default: str = "0") -> str:
    if value is None:
        return default
    try:
        if places is not None:
            quantized = _quantize(value, places)
            value = quantized if quantized is not None else value
        if value == 0:
            return "0"
        text = format(value.normalize(), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or default
    except Exception:
        return default


def _cost_from_payload(info: Dict[str, Any], fallback: Decimal | None) -> Decimal:
    for key in ("cost_usd", "cost", "avg_cost_usd", "total_cost_usd", "book_cost_usd"):
        value = _to_decimal(info.get(key))
        if value is not None:
            return value
    if fallback is not None:
        return fallback
    return _DECIMAL_ZERO


def _native_flag(symbol: str, payload: Dict[str, Any]) -> bool:
    native = bool(payload.get("native"))
    if symbol.upper() == "CRO":
        return True
    return native


def _sort_assets(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    def _key(entry: Dict[str, Any]):
        symbol = entry.get("symbol", "?").upper()
        value_dec = entry.get("_value_dec", _DECIMAL_ZERO)
        priority = 2
        if symbol == "CRO":
            priority = 0
        elif symbol == "TCRO":
            priority = 1
        return (priority, -value_dec, symbol)

    return sorted(items, key=_key)


def _build_asset(symbol: str, info: Dict[str, Any], base_ccy: str) -> Dict[str, Any]:
    amount = _to_decimal(info.get("amount") or info.get("qty"))
    price = _to_decimal(info.get("price_usd") or info.get("price"))
    value = _to_decimal(info.get("value_usd") or info.get("usd"))
    cost = _cost_from_payload(info, value)

    pnl: Decimal | None = None
    if value is not None or cost is not None:
        pnl = (value or _DECIMAL_ZERO) - (cost or _DECIMAL_ZERO)

    pnl_pct: Decimal | None = None
    if cost not in (None, _DECIMAL_ZERO):
        try:
            pnl_pct = ((value or _DECIMAL_ZERO) - cost) / cost * _DECIMAL_ONE_HUNDRED
        except Exception:
            pnl_pct = None

    asset: Dict[str, Any] = {
        "symbol": symbol,
        "amount": _decimal_to_str(amount),
        "price_usd": _decimal_to_str(price),
        "value_usd": _decimal_to_str(value),
        "cost_usd": _decimal_to_str(cost),
        "u_pnl_usd": _decimal_to_str(pnl),
        "u_pnl_pct": _decimal_to_str(_quantize(pnl_pct, 2), places=None) if pnl_pct is not None else "0",
        "native": _native_flag(symbol, info),
        "base_ccy": base_ccy,
        "raw": dict(info),
        "_amount_dec": amount or _DECIMAL_ZERO,
        "_value_dec": value or _DECIMAL_ZERO,
        "_cost_dec": cost or _DECIMAL_ZERO,
        "_pnl_dec": pnl or _DECIMAL_ZERO,
        "_pnl_pct_dec": pnl_pct,
    }
    return asset


def build_holdings_snapshot(base_ccy: str = "USD") -> Dict[str, Any]:
    """Return holdings snapshot in normalized schema.

    The function is intentionally defensive: it swallows upstream exceptions
    and always returns a dict with ``assets`` and ``totals`` keys so it can be
    used in diagnostics or GitHub Actions smoke runs.
    """

    base = (base_ccy or "USD").upper()
    try:
        raw_snapshot = holdings_snapshot()
    except Exception:
        raw_snapshot = {}

    if not isinstance(raw_snapshot, dict):
        raw_snapshot = {}

    enriched: List[Dict[str, Any]] = []
    for symbol, payload in raw_snapshot.items():
        symbol_text = str(symbol or "?").upper()
        info = payload if isinstance(payload, dict) else {}
        enriched.append(_build_asset(symbol_text, info, base))

    enriched = _sort_assets(enriched)

    assets: List[Dict[str, Any]] = []
    total_value = _DECIMAL_ZERO
    total_cost = _DECIMAL_ZERO

    for item in enriched:
        total_value += item.get("_value_dec", _DECIMAL_ZERO)
        total_cost += item.get("_cost_dec", _DECIMAL_ZERO)
        cleaned = {k: v for k, v in item.items() if not k.startswith("_")}
        assets.append(cleaned)

    u_pnl = total_value - total_cost
    u_pnl_pct: Decimal | None = None
    if total_cost != _DECIMAL_ZERO:
        try:
            u_pnl_pct = (u_pnl / total_cost) * _DECIMAL_ONE_HUNDRED
        except Exception:
            u_pnl_pct = None

    totals = {
        "value_usd": _decimal_to_str(total_value),
        "cost_usd": _decimal_to_str(total_cost),
        "u_pnl_usd": _decimal_to_str(u_pnl),
        "u_pnl_pct": _decimal_to_str(_quantize(u_pnl_pct, 2)) if u_pnl_pct is not None else "0",
        "base_ccy": base,
    }

    return {
        "base_ccy": base,
        "assets": assets,
        "totals": totals,
        "raw": raw_snapshot,
    }
