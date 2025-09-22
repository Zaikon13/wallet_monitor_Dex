from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List

@dataclass
class WatchItem:
    symbol: str
    pump: Decimal = Decimal("0.05")
    dump: Decimal = Decimal("0.05")

class WatchList:
    def __init__(self):
        self.items: Dict[str, WatchItem] = {}

    def add(self, symbol: str, pump: str = "0.05", dump: str = "0.05"):
        self.items[symbol.upper()] = WatchItem(symbol.upper(), Decimal(pump), Decimal(dump))

    def all(self) -> List[WatchItem]:
        return list(self.items.values())
