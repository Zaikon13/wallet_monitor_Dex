from __future__ import annotations

"""Wallet holdings snapshot helpers with stable schema outputs.

This module centralizes wallet snapshot building with a deterministic schema
that downstream formatters and reports can rely upon. Key properties:

* Native CRO is kept separate from tokenized CRO (tCRO) by grouping holdings
  on ``(symbol, address, is_native)`` and normalizing non-native CRO symbols to
  ``tCRO`` when necessary.
* Rows carry ``value_usd`` and optional ``cost_usd`` fields; ``unrealized_usd``
  is derived as ``value_usd - cost_usd`` whenever both inputs are available.
* Snapshot rows expose stable keys (``qty``, ``amount``, ``price_usd``,
  ``value_usd``, ``cost_usd``, ``unrealized_usd``) so Telegram/report
  formatters no longer need to guard for schema drift.
* Ordering places CRO first followed by remaining assets sorted by descending
  USD value to match UI expectations.
"""

import os
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from core.pricing import get_price_usd
from core.providers.etherscan_like import account_balance, account_tokentx, token_balance
from core.rpc import get_native_balance

__all__ = [
    "get_wallet_snapshot",
    "holdings_snapshot",
    "holdings_text",
]

_DECIMAL_ZERO = Decimal("0")
_PRICE_QUANT = Decimal("0.00000001")
_USD_QUANT = Decimal("0.0001")
_AMOUNT_QUANT = Decimal("0.000000000000000001")


def _env_addr(env: Mapping[str, str] | None = None) -> str:
    env = env or os.environ
    for key in ("WALLET_ADDRESS", "WALLETADDRESS"):
        value = (env.get(key) or "").strip()
        if value:
            return value
    return ""


def _map_from_env(key: str, env: Mapping[str, str] | None = None) -> Dict[str, str]:
    raw = (env or os.environ).get(key, "").strip()
    if not raw:
        return {}
    out: Dict[str, str] = {}
    for part in raw.split(","):
        if not part or "=" not in part:
            continue
        sym, addr = part.split("=", 1)
        sym = sym.strip()
        addr = addr.strip()
        if sym and addr:
            out[sym] = addr
    return out


def _to_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _decimal_or_zero(value: Any) -> Decimal:
    dec = _to_decimal(value)
    return dec if dec is not None else _DECIMAL_ZERO


def _decimal_to_str(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    quantized = value.normalize()
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _canonical_symbol(symbol: str, is_native: bool) -> str:
    raw = (symbol or "").strip()
    if not raw:
        return "?"
    lowered = raw.lower()
    if is_native:
        return raw.upper()
    if lowered in ("tcro", "t.cro"):
        return "tCRO"
    if lowered == "cro":
        return "tCRO"
    if lowered == "wcro":
        return "WCRO"
    return raw.upper()


def _safe_int(value: Any, default: int = 18) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _safe_price(symbol: str) -> Optional[Decimal]:
    try:
        price = get_price_usd(symbol)
    except Exception:
        return None
    dec = _to_decimal(price)
    return dec


def _cro_rpc_balance(address: str) -> Decimal:
    try:
        value = get_native_balance(address)
    except Exception:
        return _DECIMAL_ZERO
    return _decimal_or_zero(value)


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
        scale = Decimal(10) ** max(0, decimals)
        return value / scale
    except Exception:
        return None


@dataclass
class HoldingRow:
    """Internal mutable representation of a single holding row."""

    symbol: str
    address: Optional[str]
    is_native: bool
    amount: Decimal = field(default_factory=lambda: _DECIMAL_ZERO)
    price_usd: Optional[Decimal] = None
    value_usd: Optional[Decimal] = None
    cost_usd: Optional[Decimal] = None
    unrealized_usd: Optional[Decimal] = None
    sources: set[str] = field(default_factory=set)
    priority: int = 0

    def __post_init__(self) -> None:
        self.symbol = _canonical_symbol(self.symbol, self.is_native)
        addr = (self.address or "").strip().lower()
        self.address = addr or None
        self.amount = self._ensure_decimal(self.amount)
        self.price_usd = self._ensure_optional_decimal(self.price_usd)
        self.value_usd = self._ensure_optional_decimal(self.value_usd)
        self.cost_usd = self._ensure_optional_decimal(self.cost_usd)
        self.unrealized_usd = self._ensure_optional_decimal(self.unrealized_usd)
        self.sources = set(self.sources or set())

    @staticmethod
    def _ensure_decimal(value: Any) -> Decimal:
        dec = _to_decimal(value)
        if dec is None:
            return _DECIMAL_ZERO
        return dec

    @staticmethod
    def _ensure_optional_decimal(value: Any) -> Optional[Decimal]:
        dec = _to_decimal(value)
        return dec

    @property
    def key(self) -> Tuple[str, Optional[str], bool]:
        return (self.symbol, self.address, self.is_native)

    def merge(self, other: "HoldingRow") -> None:
        if self.key != other.key:
            raise ValueError("Cannot merge holdings with different identities")
        self.sources.update(other.sources)
        if other.priority > self.priority:
            self.amount = other.amount
            self.priority = other.priority
        elif other.priority == self.priority:
            self.amount += other.amount
        if other.price_usd is not None:
            self.price_usd = other.price_usd
        if other.cost_usd is not None:
            base = self.cost_usd or _DECIMAL_ZERO
            self.cost_usd = base + other.cost_usd
        if other.value_usd is not None:
            self.value_usd = other.value_usd
        if other.unrealized_usd is not None:
            self.unrealized_usd = other.unrealized_usd

    def finalize(self) -> None:
        self.amount = self.amount.quantize(_AMOUNT_QUANT, rounding=ROUND_HALF_UP)
        if self.price_usd is not None:
            self.price_usd = self.price_usd.quantize(_PRICE_QUANT, rounding=ROUND_HALF_UP)
        if self.value_usd is None and self.price_usd is not None:
            self.value_usd = (self.amount * self.price_usd).quantize(_USD_QUANT, rounding=ROUND_HALF_UP)
        elif self.value_usd is not None:
            self.value_usd = self.value_usd.quantize(_USD_QUANT, rounding=ROUND_HALF_UP)
        if self.cost_usd is not None:
            self.cost_usd = self.cost_usd.quantize(_USD_QUANT, rounding=ROUND_HALF_UP)
        if self.cost_usd is not None and self.value_usd is not None:
            self.unrealized_usd = (self.value_usd - self.cost_usd).quantize(_USD_QUANT, rounding=ROUND_HALF_UP)
        elif self.unrealized_usd is not None:
            self.unrealized_usd = self.unrealized_usd.quantize(_USD_QUANT, rounding=ROUND_HALF_UP)

    def to_snapshot_row(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "address": self.address,
            "is_native": self.is_native,
            "qty": _decimal_to_str(self.amount),
            "amount": _decimal_to_str(self.amount),
            "price_usd": _decimal_to_str(self.price_usd),
            "value_usd": _decimal_to_str(self.value_usd),
            "usd": _decimal_to_str(self.value_usd),
            "cost_usd": _decimal_to_str(self.cost_usd),
            "unrealized_usd": _decimal_to_str(self.unrealized_usd),
            "sources": sorted(self.sources),
        }


def _collect_native_rows(address: str) -> List[HoldingRow]:
    price = _safe_price("CRO")
    rpc_qty = _cro_rpc_balance(address)
    rows = [
        HoldingRow(
            symbol="CRO",
            address=address,
            is_native=True,
            amount=rpc_qty,
            price_usd=price,
            sources={"rpc"},
            priority=5,
        )
    ]
    etherscan_qty = _cro_etherscan_balance(address)
    if etherscan_qty is not None:
        rows.append(
            HoldingRow(
                symbol="CRO",
                address=address,
                is_native=True,
                amount=etherscan_qty,
                price_usd=price,
                sources={"cronoscan"},
                priority=10,
            )
        )
    return rows


def _collect_token_rows(address: str) -> List[HoldingRow]:
    addr_map = _map_from_env("TOKENS_ADDRS")
    dec_map = _map_from_env("TOKENS_DECIMALS")
    rows: List[HoldingRow] = []
    for raw_symbol, contract in addr_map.items():
        decimals = _safe_int(dec_map.get(raw_symbol))
        qty = _token_balance(contract, address, decimals)
        amount = qty if qty is not None else _DECIMAL_ZERO
        price = _safe_price(raw_symbol)
        rows.append(
            HoldingRow(
                symbol=raw_symbol,
                address=contract,
                is_native=False,
                amount=amount,
                price_usd=price,
                sources={"erc20"},
                priority=5,
            )
        )
    return rows


def _collect_history_rows(address: str, existing: MutableMapping[Tuple[str, Optional[str], bool], HoldingRow]) -> List[HoldingRow]:
    try:
        token_txs = (account_tokentx(address) or {}).get("result") or []
    except Exception:
        token_txs = []
    rows: List[HoldingRow] = []
    for tx in token_txs[-100:]:
        if not isinstance(tx, dict):
            continue
        raw_symbol = tx.get("tokenSymbol") or ""
        contract = (tx.get("contractAddress") or "").strip().lower() or None
        row = HoldingRow(
            symbol=raw_symbol,
            address=contract,
            is_native=False,
            amount=_DECIMAL_ZERO,
            sources={"history"},
            priority=-10,
        )
        if row.key in existing:
            continue
        existing[row.key] = row
        rows.append(row)
    return rows


def _group_rows(rows: Sequence[HoldingRow]) -> List[HoldingRow]:
    grouped: Dict[Tuple[str, Optional[str], bool], HoldingRow] = {}
    for row in rows:
        existing = grouped.get(row.key)
        if existing is None:
            grouped[row.key] = row
            continue
        existing.merge(row)
    return list(grouped.values())


def _order_rows(rows: Iterable[HoldingRow]) -> List[HoldingRow]:
    def order_key(row: HoldingRow) -> Tuple[int, Decimal, str, str]:
        rank = 0 if row.symbol == "CRO" and row.is_native else 1
        value = row.value_usd if row.value_usd is not None else _DECIMAL_ZERO
        return (rank, -value, row.symbol, row.address or "")

    finalized: List[HoldingRow] = []
    for row in rows:
        row.finalize()
        finalized.append(row)
    return sorted(finalized, key=order_key)


def _rows_to_snapshot(rows: Iterable[HoldingRow]) -> OrderedDict[str, Dict[str, Any]]:
    ordered_rows = _order_rows(rows)
    snapshot: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    counters: Dict[str, int] = {}
    for row in ordered_rows:
        base_key = row.symbol
        index = counters.get(base_key, 0)
        counters[base_key] = index + 1
        key = base_key if index == 0 else f"{base_key}#{index + 1}"
        snapshot[key] = row.to_snapshot_row()
    return snapshot


def _build_rows(address: str) -> List[HoldingRow]:
    rows: List[HoldingRow] = []
    rows.extend(_collect_native_rows(address))
    rows.extend(_collect_token_rows(address))
    tracker: Dict[Tuple[str, Optional[str], bool], HoldingRow] = {row.key: row for row in rows}
    rows.extend(_collect_history_rows(address, tracker))
    return rows


def get_wallet_snapshot(address: str | None = None) -> OrderedDict[str, Dict[str, Any]]:
    address = (address or _env_addr()).strip()
    if not address:
        return OrderedDict()
    rows = _build_rows(address)
    grouped = _group_rows(rows)
    return _rows_to_snapshot(grouped)


def holdings_snapshot() -> OrderedDict[str, Dict[str, Any]]:
    address = _env_addr()
    if not address:
        placeholder = HoldingRow(
            symbol="CRO",
            address=None,
            is_native=True,
            amount=_DECIMAL_ZERO,
            price_usd=_safe_price("CRO"),
            sources={"fallback"},
            priority=-20,
        )
        placeholder.finalize()
        snap = OrderedDict()
        snap["CRO"] = placeholder.to_snapshot_row()
        return snap

    rows = _build_rows(address)
    grouped = _group_rows(rows)
    has_cro = any(row.symbol == "CRO" and row.is_native for row in grouped)
    if not has_cro:
        grouped.append(
            HoldingRow(
                symbol="CRO",
                address=address,
                is_native=True,
                amount=_DECIMAL_ZERO,
                price_usd=_safe_price("CRO"),
                sources={"fallback"},
                priority=-20,
            )
        )
    return _rows_to_snapshot(grouped)


def _format_money(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    try:
        if value == 0:
            return "$0.00"
        if abs(value) >= 1:
            return f"${value:,.2f}"
        return f"${value:.6f}"
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
        if abs(value) >= 1:
            return f"{prefix}${abs(value):,.2f}"
        return f"{prefix}${abs(value):.6f}"
    except Exception:
        return "n/a"


def _usd_value(info: Mapping[str, Any]) -> Optional[Decimal]:
    return _to_decimal(info.get("value_usd") or info.get("usd"))


def _price_value(info: Mapping[str, Any]) -> Optional[Decimal]:
    return _to_decimal(info.get("price_usd") or info.get("price"))


def _delta_value(info: Mapping[str, Any]) -> Optional[Decimal]:
    for key in ("unrealized_usd", "pnl_usd", "delta_usd", "pnl", "change_usd"):
        val = _to_decimal(info.get(key))
        if val is not None:
            return val
    return None


def holdings_text(snapshot: Mapping[str, Dict[str, Any]] | None = None) -> str:
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
    for key, info in data.items():
        symbol = str((info or {}).get("symbol") or key)
        qty = _to_decimal((info or {}).get("qty") or (info or {}).get("amount")) or Decimal("0")
        price_val = _price_value(info or {})
        usd_val = _usd_value(info or {})
        delta_val = _delta_value(info or {})
        if usd_val is not None:
            total_usd += usd_val
        lines.append(
            " - {sym:<8} {qty_str} @ {price} → USD {usd} (Δ {delta})".format(
                sym=symbol,
                qty_str=_format_qty(qty),
                price=_format_money(price_val),
                usd=_format_money(usd_val),
                delta=_format_delta(delta_val),
            )
        )

    lines.append("")
    lines.append(f"Total ≈ {_format_money(total_usd)}")
    return "\n".join(lines)
