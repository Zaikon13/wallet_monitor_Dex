from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

from telegram.commands import (
    handle_daily,
    handle_diag,
    handle_holdings,
    handle_pnl,
    handle_status,
    handle_totals,
    handle_tx,
    handle_weekly,
)

_COOLDOWN_SEC = 5
_last_exec: Dict[tuple[str, Optional[int]], float] = {}


def _cooldown_key(command: str, chat_id: Optional[int]) -> tuple[str, Optional[int]]:
    return command, chat_id


def _under_cooldown(command: str, chat_id: Optional[int]) -> bool:
    key = _cooldown_key(command, chat_id)
    now = time.time()
    last = _last_exec.get(key, 0.0)
    if now - last < _COOLDOWN_SEC:
        return True
    _last_exec[key] = now
    return False


def _parse_weekly_args(args: List[str]) -> int:
    if not args:
        return 7
    try:
        value = int(args[0])
    except (TypeError, ValueError):
        return 7
    return max(1, min(31, value))


def _parse_tx_args(args: List[str]) -> tuple[Optional[str], Optional[str]]:
    symbol = None
    day = None
    for arg in args:
        if arg and any(ch.isdigit() for ch in arg) and "-" in arg:
            day = arg
        elif symbol is None:
            symbol = arg
    return symbol, day


def dispatch(text: str, chat_id: Optional[int] = None) -> List[str]:
    if not text:
        return []
    parts = text.strip().split()
    if not parts:
        return []

    command = parts[0].lower()
    args = parts[1:]

    handlers: Dict[str, Callable[[List[str]], List[str]]] = {
        "/diag": lambda _args: [handle_diag()],
        "/status": lambda _args: [handle_status()],
        "/holdings": lambda _args: [handle_holdings()],
        "/totals": lambda _args: [handle_totals()],
        "/daily": lambda _args: [handle_daily()],
        "/weekly": lambda a: [handle_weekly(_parse_weekly_args(a))],
        "/pnl": lambda a: [handle_pnl(a[0]) if a else handle_pnl(None)],
        "/tx": lambda a: [handle_tx(*_parse_tx_args(a))],
    }

    handler = handlers.get(command)
    if handler is None:
        return []

    if _under_cooldown(command, chat_id):
        return ["⌛ cooldown"]

    result = handler(args)
    if not result:
        return []

    output: List[str] = []
    for item in result:
        if isinstance(item, str):
            output.append(item)
    return output
