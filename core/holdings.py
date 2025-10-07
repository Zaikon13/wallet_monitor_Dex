from __future__ import annotations

"""Wallet holdings snapshot helpers.

This module builds sanitized wallet snapshots that always include CRO seeded
from the RPC balance. Environment fallbacks are used throughout to keep the
module side-effect free under missing configuration.
"""

import os
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Mapping, Optional

from core.providers.etherscan_like import (
    account_balance,
    account_tokentx,
    token_balance,
)
from core.pricing import get_price_usd
from core.rpc import get_native_balance

__all__ = [
    "_env_addr",
    "get_wallet_snapshot",
    "holdings_snapshot",
    "holdings_text",
]

_DECIMAL_ZERO = Decimal("0")
_USD_QUANTIZE = Decimal("0.0001")


def _env_addr(env: Mapping[str, str] | None = None) -> str:
    """Return the first non-empty wallet address from the environment."""

    env = env or os.environ
    for key in ("WALLET_ADDRESS", "WALLETADDRESS"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    return ""


def _map_from_env(key: str, env: Mapping[str, str] | None = None) -> Dict[str, str]:
    """Parse an env var like ``SYMA=0x1234,SYMB=0xabcd`` into a dict."""

    raw = (env or os.environ).get(key, "").strip()
    if not raw:
        return {}

    out: Dict[str, str] = {}
    for part in raw.split(","):
        if not part or "=" not in part:
            continue
        sym, addr = part.split("=", 1)
        sym = sym.strip().upper()
        addr = addr.strip()
        if sym and addr:
            out[sym] = addr
    return out


def _to_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _decimal_to_str(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _normalize_symbol(symbol: str) -> str:
    raw = (symbol or "").strip()
    if not raw:
        return "?"
    return raw.upper()


def _sanitize_snapshot(raw: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    sanitized: Dict[str, Dict[str, Any]] = {}
    for symbol, data in (raw or {}).items():
        key = _normalize_symbol(symbol)
        payload = dict(data or {})
        payload.setdefault("symbol", key)
        sanitized[key] = payload
    return sanitized


def _safe_mul(a: Optional[Decimal], b: Optional[Decimal]) -> Optional[Decimal]:
    if a is None or b is None:
        return None
    try:
        return (a * b).quantize(_USD_QUANTIZE)
    except Exception:
        return None


def _safe_price(symbol: str) -> Optional[Decimal]:
    try:
        price = get_price_usd(symbol)
    except Exception:
        return None
    return _to_decimal(price)


def _cro_rpc_balance(address: str) -> Decimal:
    try:
        value = get_native_balance(address)
    except Exception:
        return _DECIMAL_ZERO
    dec = _to_decimal(value)
    return dec if dec is not None else _DECIMAL_ZERO


def _cro_etherscan_balance(address: str) -> Optional[Decimal]:
    try:
        raw = account_balance(address).get("result")
    except Exception:
        return None
    dec = _to_decimal(raw)
    if dec is None:
        return None
    try:
        return dec / (Decimal(10) ** 18)
    except Exception:
        return None


def _token_balance(contract: str, address: str, decimals: int) -> Optional[Decimal]:
    try:
        raw = token_balance(contract, address).get("result")
    except Exception:
        return None
    value = _to_decimal(raw)
    if value is None:
        return None
    try:
        scale = Decimal(10) ** max(0, int(decimals))
        return value / scale
    except Exception:
        return None


def get_wallet_snapshot(address: str | None = None) -> Dict[str, Dict[str, Optional[str]]]:
    """Return a raw snapshot of holdings (includes CRO + configured tokens)."""

    address = (address or _env_addr()).strip()
    if not address:
        return {}

    snapshot: Dict[str, Dict[str, Optional[str]]] = {}

    cro_qty = _cro_rpc_balance(address)
    cro_price = _safe_price("CRO")
    cro_usd = _safe_mul(cro_qty, cro_price)
    snapshot["CRO"] = {
        "qty": _decimal_to_str(cro_qty),
        "price_usd": (_decimal_to_str(cro_price) if cro_price is not None else None),
        "usd": (_decimal_to_str(cro_usd) if cro_usd is not None else None),
    }

    etherscan_qty = _cro_etherscan_balance(address)
    if etherscan_qty is not None:
        etherscan_price = cro_price or _safe_price("CRO")
        etherscan_usd = _safe_mul(etherscan_qty, etherscan_price)
        snapshot["CRO"].update(
            {
                "qty": _decimal_to_str(etherscan_qty),
                "price_usd": (
                    _decimal_to_str(etherscan_price)
                    if etherscan_price is not None
                    else snapshot["CRO"].get("price_usd")
                ),
                "usd": (
                    _decimal_to_str(etherscan_usd)
                    if etherscan_usd is not None
                    else snapshot["CRO"].get("usd")
                ),
            }
        )

    addr_map = _map_from_env("TOKENS_ADDRS")
    dec_map = _map_from_env("TOKENS_DECIMALS")
    for symbol, contract in addr_map.items():
        decimals = int(dec_map.get(symbol, "18") or 18)
        qty = _token_balance(contract, address, decimals)
        if qty is None:
            snapshot.setdefault(symbol, {"qty": "0", "price_usd": None, "usd": None})
            continue

        price = _safe_price(symbol)
        usd = _safe_mul(qty, price)
        snapshot[symbol] = {
            "qty": _decimal_to_str(qty),
            "price_usd": (_decimal_to_str(price) if price is not None else None),
            "usd": (_decimal_to_str(usd) if usd is not None else None),
        }

    try:
        token_txs = (account_tokentx(address) or {}).get("result") or []
    except Exception:
        token_txs = []

    for tx in token_txs[-50:]:
        sym = _normalize_symbol(tx.get("tokenSymbol", ""))
        snapshot.setdefault(sym, {"qty": "?", "price_usd": None, "usd": None})

    return snapshot


def holdings_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return a sanitized holdings snapshot with CRO guaranteed via RPC."""

    address = _env_addr()

    cro_entry: Dict[str, Any] = {"qty": "0", "price_usd": None, "usd": None, "symbol": "CRO"}
    if address:
        qty = _cro_rpc_balance(address)
        price = _safe_price("CRO")
        usd = _safe_mul(qty, price)
        cro_entry.update(
            {
                "qty": _decimal_to_str(qty),
                "price_usd": (_decimal_to_str(price) if price is not None else None),
                "usd": (_decimal_to_str(usd) if usd is not None else None),
            }
        )

    try:
        raw = get_wallet_snapshot(address or None)
    except Exception:
        raw = {}

    sanitized = _sanitize_snapshot(raw)
    merged_cro = sanitized.get("CRO", {}).copy()
    merged_cro.update({k: v for k, v in cro_entry.items() if v is not None})
    merged_cro.setdefault("symbol", "CRO")
    merged_cro.setdefault("qty", cro_entry.get("qty", "0"))
    merged_cro.setdefault("price_usd", cro_entry.get("price_usd"))
    merged_cro.setdefault("usd", cro_entry.get("usd"))
    sanitized["CRO"] = merged_cro

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
    for key in ("pnl_usd", "delta_usd", "pnl", "change_usd", "unrealized_usd"):
        val = _to_decimal(info.get(key))
        if val is not None:
            return val
    return None


def _ordered_symbols(snapshot: Dict[str, Dict[str, Any]]) -> Iterable[str]:
    if not snapshot:
        return []

    symbols = list(snapshot.keys())
    ordered: list[str] = []
    if "CRO" in symbols:
        ordered.append("CRO")
        symbols.remove("CRO")

    symbols.sort(
        key=lambda sym: (
            _usd_value(snapshot.get(sym, {})) or _DECIMAL_ZERO,
            sym,
        ),
        reverse=True,
    )
    ordered.extend(symbols)
    return ordered


def holdings_text(snapshot: Dict[str, Dict[str, Any]] | None = None) -> str:
    """Format holdings with CRO-first ordering and defensive fallbacks."""

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
