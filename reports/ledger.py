from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

from core.tz import now_gr, ymd

LEDGER_DIR = Path(os.getenv("LEDGER_DIR", "./.ledger")).expanduser()
LEDGER_DIR.mkdir(parents=True, exist_ok=True)
COST_BASIS_FILE = LEDGER_DIR / "cost_basis.json"

_DECIMAL_KEYS = ("qty", "price_usd", "usd", "fee_usd", "realized_usd")


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _serialize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key, value in entry.items():
        if key in _DECIMAL_KEYS and value is not None:
            payload[key] = str(_to_decimal(value))
        else:
            payload[key] = value
    return payload


def _deserialize_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    for key, value in entry.items():
        if key in _DECIMAL_KEYS:
            data[key] = _to_decimal(value)
        else:
            data[key] = value
    if "realized_usd" not in data:
        data["realized_usd"] = Decimal("0")
    return data


def _day_from_entry(entry: Dict[str, Any]) -> Optional[str]:
    ts = entry.get("time") or entry.get("timestamp")
    if ts is None:
        return None
    try:
        ts_int = int(float(ts))
    except (TypeError, ValueError):
        return None
    dt = datetime.fromtimestamp(ts_int, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def data_file_for_day(day: str) -> Path:
    return LEDGER_DIR / f"{day}.json"


def data_file_for_today() -> Path:
    return data_file_for_day(ymd())


def _read_day_raw(day: str) -> List[Dict[str, Any]]:
    path = data_file_for_day(day)
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_day(day: str, entries: Iterable[Dict[str, Any]]) -> None:
    payload = [_serialize_entry(entry) for entry in entries]
    data_file_for_day(day).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _is_valid_day(name: str) -> bool:
    try:
        datetime.strptime(name, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def list_days() -> List[str]:
    return sorted(
        [
            path.stem
            for path in LEDGER_DIR.glob("*.json")
            if path.is_file() and _is_valid_day(path.stem)
        ]
    )


def read_ledger(day: str) -> List[Dict[str, Any]]:
    raw_entries = _read_day_raw(day)
    entries = [_deserialize_entry(entry) for entry in raw_entries]
    entries.sort(key=lambda entry: entry.get("time") or 0)
    return entries


def iter_all_entries() -> Iterator[Dict[str, Any]]:
    for day in list_days():
        for entry in read_ledger(day):
            yield entry


def append_ledger(day_or_entry: Any, entry: Optional[Dict[str, Any]] = None) -> None:
    if entry is None:
        if not isinstance(day_or_entry, dict):
            raise ValueError("append_ledger(entry) requires a dict entry")
        entry = dict(day_or_entry)
        day = entry.pop("day", None) or _day_from_entry(entry) or ymd()
    else:
        day = str(day_or_entry)
        entry = dict(entry)

    normalized = _deserialize_entry(entry)
    normalized.setdefault("wallet", entry.get("wallet"))
    if normalized.get("realized_usd") is None:
        normalized["realized_usd"] = Decimal("0")

    day_entries = read_ledger(day)
    day_entries.append(normalized)
    day_entries.sort(key=lambda item: item.get("time") or 0)
    _write_day(day, day_entries)


def _clone_basis(basis: Dict[str, List[Dict[str, Decimal]]]) -> Dict[str, List[Dict[str, Decimal]]]:
    cloned: Dict[str, List[Dict[str, Decimal]]] = {}
    for asset, lots in basis.items():
        cloned[asset] = [
            {"qty": Decimal(lot["qty"]), "usd": Decimal(lot["usd"])} for lot in lots
        ]
    return cloned


def _consume_lots(lots: List[Dict[str, Decimal]], qty: Decimal) -> Decimal:
    remaining = qty
    cost = Decimal("0")
    while remaining > 0 and lots:
        lot = lots[0]
        lot_qty = lot["qty"]
        lot_usd = lot["usd"]
        if lot_qty <= 0:
            lots.pop(0)
            continue
        take = lot_qty if lot_qty <= remaining else remaining
        proportion = take / lot_qty if lot_qty else Decimal("0")
        cost += lot_usd * proportion
        lot["qty"] = lot_qty - take
        lot["usd"] = lot_usd - (lot_usd * proportion)
        remaining -= take
        if lot["qty"] <= Decimal("0"):
            lots.pop(0)
    return cost


def replay_cost_basis_over_entries(
    entries: Iterable[Dict[str, Any]],
    basis: Optional[Dict[str, List[Dict[str, Decimal]]]] = None,
) -> Tuple[Dict[str, List[Dict[str, Decimal]]], Dict[str, Decimal], List[Dict[str, Any]]]:
    basis_state = _clone_basis(basis or {})
    realized_totals: Dict[str, Decimal] = {}
    annotated_entries: List[Dict[str, Any]] = []

    for raw in entries or []:
        entry = _deserialize_entry(dict(raw))
        asset = str(entry.get("asset") or "?").upper()
        side = str(entry.get("side") or "").upper()
        qty = _to_decimal(entry.get("qty"))
        usd = _to_decimal(entry.get("usd"))
        fee = _to_decimal(entry.get("fee_usd"))

        lots = basis_state.setdefault(asset, [])
        realized = Decimal("0")

        if side == "IN" and qty > 0:
            lots.append({"qty": qty, "usd": usd + fee})
        elif side == "OUT" and qty > 0:
            proceeds = usd - fee
            cost = _consume_lots(lots, qty)
            realized = proceeds - cost
        entry["realized_usd"] = realized
        realized_totals[asset] = realized_totals.get(asset, Decimal("0")) + realized
        annotated_entries.append(entry)

    return basis_state, realized_totals, annotated_entries


def update_cost_basis() -> None:
    basis: Dict[str, List[Dict[str, Decimal]]] = {}
    for day in list_days():
        entries = read_ledger(day)
        basis, _, annotated = replay_cost_basis_over_entries(entries, basis)
        _write_day(day, annotated)

    payload = {
        asset: [{"qty": str(lot["qty"]), "usd": str(lot["usd"]) } for lot in lots]
        for asset, lots in basis.items()
    }
    COST_BASIS_FILE.write_text(
        json.dumps({"updated_at": now_gr().isoformat(), "basis": payload}, indent=2),
        encoding="utf-8",
    )


__all__ = [
    "append_ledger",
    "data_file_for_today",
    "iter_all_entries",
    "list_days",
    "read_ledger",
    "replay_cost_basis_over_entries",
    "update_cost_basis",
]
