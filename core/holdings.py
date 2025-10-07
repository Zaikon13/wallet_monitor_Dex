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
from core.rpc import get_native_balance

def _env_addr() -> str:
    """Return wallet address from env (supports WALLET_ADDRESS & WALLETADDRESS)."""
    for key in ("WALLET_ADDRESS", "WALLETADDRESS"):
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


def _map_from_env(key: str) -> Dict[str, str]:
    """Parse an env var like 'SYMA=0x1234,SYMB=0xabcd' into a dict."""
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
    if not raw:
        return "?"
    return raw.upper()  # normalized; no tCRO special casing


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
    address = (address or _env_addr()).strip()
    if not address:
        return {}

    snap: Dict[str, Dict[str, Optional[str]]] = {}

    # Seed CRO balance via RPC before touching ERC-20 discovery so it's always present.
    cro_qty = Decimal("0")
    try:
        cro_qty = Decimal(str(get_native_balance(address)))
    except Exception:
        cro_qty = Decimal("0")

    cro_price = get_price_usd("CRO")
    cro_usd: Optional[Decimal] = None
    if cro_price is not None:
        try:
            cro_usd = (cro_qty * cro_price).quantize(Decimal("0.0001"))
        except Exception:
            cro_usd = None

    snap["CRO"] = {
        "qty": str(cro_qty.normalize()),
        "price_usd": (str(cro_price) if cro_price is not None else None),
        "usd": (str(cro_usd) if cro_usd is not None else None),
    }

    # Legacy fallback using etherscan-like API in case RPC silently fails.
    try:
        bal = account_balance(address).get("result")
        if bal is not None:
            cro_etherscan = Decimal(str(bal)) / (Decimal(10) ** 18)
            if cro_etherscan != cro_qty:
                px = cro_price or get_price_usd("CRO")
                usd_val: Optional[Decimal] = None
                if px is not None:
                    try:
                        usd_val = (cro_etherscan * px).quantize(Decimal("0.0001"))
                    except Exception:
                        usd_val = None
                snap["CRO"].update(
                    {
                        "qty": str(cro_etherscan.normalize()),
                        "price_usd": (str(px) if px is not None else snap["CRO"].get("price_usd")),
                        "usd": (str(usd_val) if usd_val is not None else snap["CRO"].get("usd")),
                    }
                )
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
    """Return a sanitized snapshot dict suitable for formatting.
    Guarantees a CRO entry seeded from RPC, then merges with discovered data.
    """
    address = _env_addr()

    # Seed CRO via RPC (never raise)
    cro_entry: Dict[str, Any] = {"qty": "0", "price_usd": None, "usd": None}
    if address:
        try:
            balance_cro = Decimal(str(get_native_balance(address)))
        except Exception:
            balance_cro = Decimal("0")
        try:
            px = get_price_usd("CRO")
        except Exception:
            px = None
        try:
            usd = (balance_cro * px).quantize(Decimal("0.0001")) if px is not None else None
        except Exception:
            usd = None
        cro_entry = {
            "qty": str(balance_cro.normalize()),
            "price_usd": (str(px) if px is not None else None),
            "usd": (str(usd) if usd is not None else None),
        }

    # Build raw snapshot (may include CRO/tokens); never raise
    try:
        raw = get_wallet_snapshot(address=address or None)
    except Exception:
        raw = {}

    # Sanitize & merge CRO (RPC first, then enrich with discovered data)
    sanitized = _sanitize_snapshot(raw)
    existing_cro = sanitized.get("CRO", {})
    merged_cro = dict(existing_cro)
    for k, v in cro_entry.items():
        if v is not None:
            merged_cro[k] = v
    merged_cro["qty"] = cro_entry.get("qty", merged_cro.get("qty", "0"))
    merged_cro.setdefault("symbol", "CRO")
    sanitized["CRO"] = merged_cro

    # Always guarantee a CRO row
    if "CRO" not in sanitized:
        sanitized["CRO"] = {"symbol": "CRO", "qty": "0", "price_usd": None, "usd": None}

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
    if not snapshot:
        return []

    keys = list(snapshot.keys())
    ordered: list[str] = []

    if "CRO" in keys:
        ordered.append("CRO")
        keys.remove("CRO")

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
