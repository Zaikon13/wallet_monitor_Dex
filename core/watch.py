# core/watch.py
from __future__ import annotations
from decimal import Decimal
from typing import Dict, Iterable, Tuple, List
import threading

from core.pricing import get_price_usd
from core.alerts import notify_alert

class PriceWatcher:
    """
    Lightweight price watcher with in-memory state.
    Keeps last prices per symbol and reports threshold moves.
    """
    def __init__(self, symbols: Iterable[str] = ("CRO",), pump="0.05", dump="0.05"):
        self._lock = threading.Lock()
        self._last: Dict[str, Decimal] = {}
        self._symbols = { (s or "").upper() for s in symbols }
        self._pump = Decimal(str(pump))
        self._dump = Decimal(str(dump))

    def add(self, symbol: str) -> None:
        with self._lock:
            self._symbols.add((symbol or "").upper())

    def symbols(self) -> List[str]:
        with self._lock:
            return sorted(self._symbols)

    def poll_once(self) -> List[Tuple[str, Decimal, Decimal]]:
        """
        Returns list of (symbol, price, delta) when |delta| >= thresholds;
        also emits telegram alerts via notify_alert.
        """
        trigs: List[Tuple[str, Decimal, Decimal]] = []
        with self._lock:
            for s in list(self._symbols):
                price = Decimal(str(get_price_usd(s) or 0))
                prev = self._last.get(s)
                self._last[s] = price
                if prev and prev > 0 and price > 0:
                    delta = (price - prev) / prev
                    if delta >= self._pump or delta <= -self._dump:
                        arrow = "↑" if delta > 0 else "↓"
                        notify_alert(f"{s} {arrow} {delta:.2%} → {price}")
                        trigs.append((s, price, delta))
        return trigs
