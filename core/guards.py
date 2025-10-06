from __future__ import annotations

import os
import time
from decimal import Decimal
from typing import Any, Dict, Optional, Set

__all__ = [
    "make_guards_from_env",
    "configure_guards",
    "mark_trade",
    "set_holdings",
    "should_alert",
]


DEFAULT_CONFIG: Dict[str, Any] = {
    "window_minutes": 60,
    "pump_pct": 20.0,
    "drop_pct": -12.0,
    "trail_drop_pct": -8.0,
    "min_volume": 0.0,
    "min_liquidity": 0.0,
    "spike_threshold": 8.0,
    "cooldown_seconds": 30,
}

_CONFIG: Dict[str, Any] = dict(DEFAULT_CONFIG)
_last: Dict[str, float] = {}
peaks: Dict[str, float] = {}
traded: Set[str] = set()
holdings: Set[str] = set()


def _to_float(value: Any, default: float) -> float:
    try:
        if isinstance(value, Decimal):
            return float(value)
        return float(str(value))
    except (TypeError, ValueError):
        return float(default)


def _to_int(value: Any, default: int) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return int(default)


def make_guards_from_env(env: os._Environ[str] | Dict[str, str] = os.environ) -> Dict[str, Any]:
    """Return guard thresholds computed from the environment."""
    cfg = dict(DEFAULT_CONFIG)

    cfg["window_minutes"] = _to_int(env.get("GUARD_WINDOW_MIN"), cfg["window_minutes"])
    cfg["pump_pct"] = _to_float(env.get("GUARD_PUMP_PCT"), cfg["pump_pct"])
    cfg["drop_pct"] = _to_float(env.get("GUARD_DROP_PCT"), cfg["drop_pct"])
    cfg["trail_drop_pct"] = _to_float(env.get("GUARD_TRAIL_DROP_PCT"), cfg["trail_drop_pct"])
    cfg["min_volume"] = _to_float(env.get("MIN_VOLUME_FOR_ALERT"), cfg["min_volume"])
    cfg["min_liquidity"] = _to_float(env.get("DISCOVER_MIN_LIQ_USD"), cfg["min_liquidity"])
    cfg["spike_threshold"] = _to_float(env.get("SPIKE_THRESHOLD"), cfg["spike_threshold"])

    cooldown_raw = env.get("ALERTS_INTERVAL_MINUTES")
    if cooldown_raw:
        minutes = _to_int(cooldown_raw, cfg["window_minutes"])
        cfg["cooldown_seconds"] = max(10, minutes * 60)
    else:
        cfg["cooldown_seconds"] = DEFAULT_CONFIG["cooldown_seconds"]

    return cfg


def configure_guards(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Update the active guard configuration and return the applied values."""
    global _CONFIG
    applied = dict(DEFAULT_CONFIG)
    if config:
        for key, value in config.items():
            if key in applied:
                if key == "window_minutes":
                    applied[key] = _to_int(value, applied[key])
                elif key == "cooldown_seconds":
                    applied[key] = max(1, _to_int(value, applied[key]))
                else:
                    applied[key] = _to_float(value, applied[key])
    _CONFIG = applied
    return dict(_CONFIG)


def mark_trade(symbol: str, side: str) -> None:
    traded.add((symbol or "").upper())


def set_holdings(symbols: Set[str]) -> None:
    global holdings
    holdings = {s.upper() for s in (symbols or set())}


def _cool_ok(sym: str) -> bool:
    now = time.time()
    last = _last.get(sym, 0.0)
    cooldown = float(_CONFIG.get("cooldown_seconds", DEFAULT_CONFIG["cooldown_seconds"]))
    if now - last < cooldown:
        return False
    _last[sym] = now
    return True


def should_alert(ev: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    sym = str(ev.get("symbol") or "").upper()
    if not sym:
        return None

    price = ev.get("price_usd")
    change_pct = ev.get("change_pct")
    volume = ev.get("volume24_usd")
    liquidity = ev.get("liquidity_usd")
    is_new = bool(ev.get("is_new_pair", False))
    spike = ev.get("spike_pct")

    min_volume = float(_CONFIG.get("min_volume", DEFAULT_CONFIG["min_volume"]))
    min_liq = float(_CONFIG.get("min_liquidity", DEFAULT_CONFIG["min_liquidity"]))
    spike_threshold = float(_CONFIG.get("spike_threshold", DEFAULT_CONFIG["spike_threshold"]))
    pump_pct = float(_CONFIG.get("pump_pct", DEFAULT_CONFIG["pump_pct"]))
    drop_pct = float(_CONFIG.get("drop_pct", DEFAULT_CONFIG["drop_pct"]))
    trail_drop_pct = float(_CONFIG.get("trail_drop_pct", DEFAULT_CONFIG["trail_drop_pct"]))

    if volume is not None and _to_float(volume, 0.0) < min_volume:
        return None
    if liquidity is not None and _to_float(liquidity, 0.0) < min_liq:
        return None

    in_scope = sym in holdings or sym in traded or (
        is_new and spike is not None and _to_float(spike, 0.0) >= spike_threshold
    )
    if not in_scope:
        return None

    if price is not None:
        try:
            price_val = float(price)
            peak = peaks.get(sym)
            if peak is None or price_val > peak:
                peaks[sym] = price_val
        except (TypeError, ValueError):
            pass

    action: Optional[str] = None
    if change_pct is not None:
        try:
            change_val = float(change_pct)
        except (TypeError, ValueError):
            change_val = 0.0
        if change_val >= pump_pct:
            action = "BUY_MORE"
        elif change_val <= drop_pct:
            action = "SELL"

    if action is None and price is not None:
        try:
            price_val = float(price)
            peak = peaks.get(sym)
            if peak and peak > 0:
                drawdown = (price_val - peak) / peak * 100
                if drawdown <= trail_drop_pct:
                    action = "SELL"
        except (TypeError, ValueError):
            pass

    if not action or not _cool_ok(sym):
        return None

    enriched = dict(ev)
    enriched["guard_action"] = action
    return enriched
