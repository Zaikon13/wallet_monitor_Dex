from __future__ import annotations

import threading
import time
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional

_state_lock = threading.Lock()
_state: Dict[str, Any] = {
    "last_tick_ts": None,
    "last_wallet_poll_ts": None,
    "last_wallet_poll_success": None,
    "last_rpc_ok_ts": None,
    "last_rpc_error": None,
    "snapshot": {},
    "snapshot_total_usd": Decimal("0"),
    "snapshot_ts": None,
    "queue_sizes": {},
    "last_cost_basis_update": None,
}


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def update_snapshot(snapshot: Dict[str, Dict[str, Any]], timestamp: Optional[float] = None) -> None:
    with _state_lock:
        total = Decimal("0")
        for item in (snapshot or {}).values():
            total += _to_decimal(item.get("usd") or item.get("value_usd") or 0)
        _state["snapshot"] = deepcopy(snapshot or {})
        _state["snapshot_total_usd"] = total
        _state["snapshot_ts"] = timestamp or time.time()


def get_snapshot() -> Dict[str, Dict[str, Any]]:
    with _state_lock:
        return deepcopy(_state.get("snapshot", {}))


def note_tick() -> None:
    with _state_lock:
        _state["last_tick_ts"] = time.time()


def note_wallet_poll(success: bool, error: Optional[str] = None) -> None:
    with _state_lock:
        now = time.time()
        _state["last_wallet_poll_ts"] = now
        _state["last_wallet_poll_success"] = success
        if success:
            _state["last_rpc_ok_ts"] = now
            _state["last_rpc_error"] = None
        else:
            _state["last_rpc_error"] = error or "unknown"


def note_cost_basis_update() -> None:
    with _state_lock:
        _state["last_cost_basis_update"] = time.time()


def set_queue_size(name: str, size: int) -> None:
    with _state_lock:
        queues = dict(_state.get("queue_sizes") or {})
        queues[name] = int(size)
        _state["queue_sizes"] = queues


def get_state() -> Dict[str, Any]:
    with _state_lock:
        snapshot_copy = deepcopy(_state.get("snapshot", {}))
        data = dict(_state)
        data["snapshot"] = snapshot_copy
        return data
