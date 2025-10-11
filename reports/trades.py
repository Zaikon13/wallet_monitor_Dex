# reports/trades.py
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Iterable, Tuple

# --- Time helpers (no external deps) ---
def _tz() -> timezone:
    # Respect repo env default
    tz_str = os.getenv("TZ", "Europe/Athens")
    # We won't ship a full tz database here; use a fixed offset fallback if needed.
    # If your core.tz provides proper utilities, feel free to replace these two helpers with imports.
    # For Greece (Athens) we assume UTC+2/UTC+3. To avoid DST complexity, we take current offset from system.
    # Railway sets TZ=Europe/Athens; Python will honor it for localtime conversions.
    # We'll treat "local" as system local (which is TZ=Europe/Athens in Railway env).
    return datetime.now().astimezone().tzinfo or timezone(timedelta(hours=3))

def _today_window() -> Tuple[datetime, datetime]:
    now = datetime.now(tz=_tz())
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start, end

def _parse_ts(ts: str) -> datetime:
    # Accept ISO or epoch seconds or ms
    ts = str(ts).strip()
    try:
        if ts.isdigit():
            # epoch (seconds or ms)
            iv = int(ts)
            if iv > 10_000_000_000:  # ms
                iv //= 1000
            return datetime.fromtimestamp(iv, tz=_tz())
        # ISO
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz())
        else:
            dt = dt.astimezone(_tz())
        return dt
    except Exception:
        # As last resort, treat as local naive
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_tz())

# --- Data model ---
@dataclass
class Trade:
    ts: datetime
    symbol: str
    side: str  # "BUY" | "SELL"
    qty: float
    price: float
    fee: float = 0.0
    tx: Optional[str] = None
    chain: Optional[str] = None

    @property
    def gross(self) -> float:
        return self.qty * self.price * (1 if self.side.upper() == "SELL" else -1)

# --- Ledger loaders (flexible) ---
def _from_reports_ledger() -> List[Trade]:
    """
    Tries to get trades from reports.ledger using a few common function names.
    Expected entry keys (best-effort): ts/timestamp, symbol, side, qty/amount, price, fee, tx/tx_hash, chain.
    """
    try:
        from reports import ledger as rledger  # type: ignore
    except Exception:
        return []

    candidates = [
        "get_ledger_entries",
        "load_ledger",
        "read_ledger",
        "all_entries",
    ]
    entries = None
    for name in candidates:
        if hasattr(rledger, name):
            try:
                entries = getattr(rledger, name)()
                break
            except Exception:
                continue
    if entries is None:
        return []

    out: List[Trade] = []
    for e in entries:
        try:
            ts = _parse_ts(e.get("ts") or e.get("timestamp"))
            symbol = str(e.get("symbol") or e.get("asset") or e.get("token") or "").upper()
            side = str(e.get("side") or e.get("action") or "").upper()
            qty = float(e.get("qty") or e.get("amount") or 0)
            price = float(e.get("price") or e.get("unit_price") or 0)
            fee = float(e.get("fee") or 0)
            tx = e.get("tx") or e.get("tx_hash") or e.get("hash")
            chain = e.get("chain") or e.get("network") or "cronos"
            if symbol:
                out.append(Trade(ts, symbol, side, qty, price, fee, tx, chain))
        except Exception:
            # skip malformed rows
            continue
    return out

def _from_csv_fallback() -> List[Trade]:
    """
    Optional CSV fallback: data/ledger.csv with columns:
    ts,symbol,side,qty,price,fee,tx,chain
    """
    path = os.path.join("data", "ledger.csv")
    if not os.path.exists(path):
        return []
    out: List[Trade] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for e in reader:
            try:
                ts = _parse_ts(e.get("ts") or e.get("timestamp"))
                symbol = str(e.get("symbol") or "").upper()
                side = str(e.get("side") or "").upper()
                qty = float(e.get("qty") or 0)
                price = float(e.get("price") or 0)
                fee = float(e.get("fee") or 0)
                tx = e.get("tx") or e.get("tx_hash")
                chain = e.get("chain") or "cronos"
                if symbol:
                    out.append(Trade(ts, symbol, side, qty, price, fee, tx, chain))
            except Exception:
                continue
    return out

def load_trades() -> List[Trade]:
    rows = _from_reports_ledger()
    if rows:
        return sorted(rows, key=lambda t: t.ts)
    # fallback (optional)
    rows = _from_csv_fallback()
    return sorted(rows, key=lambda t: t.ts)

# --- Filters ---
def trades_in_window(start: datetime, end: datetime, symbols: Optional[Iterable[str]] = None) -> List[Trade]:
    syms = set(s.upper() for s in symbols) if symbols else None
    out: List[Trade] = []
    for t in load_trades():
        if start <= t.ts < end and (syms is None or t.symbol in syms):
            out.append(t)
    return out

def todays_trades(symbols: Optional[Iterable[str]] = None) -> List[Trade]:
    start, end = _today_window()
    return trades_in_window(start, end, symbols)

# --- Realized PnL (FIFO per symbol, full history, return only today's realized) ---
@dataclass
class RealizedFill:
    ts: datetime
    symbol: str
    qty: float
    sell_price: float
    buy_cost: float
    pnl: float
    tx: Optional[str]

@dataclass
class RealizedSummary:
    window_start: datetime
    window_end: datetime
    per_symbol: Dict[str, Dict[str, float]]  # {symbol: {"realized": x, "fees": y, "qty_sold": z}}
    fills: List[RealizedFill]
    total_realized: float
    total_fees: float

def _fifo_realized_today(trades: List[Trade]) -> RealizedSummary:
    # Build FIFO lots using full history
    all_trades = load_trades()  # sorted
    lots: Dict[str, List[Tuple[float, float]]] = {}  # symbol -> list of (qty_remaining, unit_cost)
    fills: List[RealizedFill] = []

    # Preload buys/sells up to end of "today"
    start, end = _today_window()

    # Process every trade chronologically
    for t in all_trades:
        sym = t.symbol
        lots.setdefault(sym, [])
        if t.side == "BUY":
            lots[sym].append([t.qty, t.price])  # mutable qty
        elif t.side == "SELL":
            sell_qty = t.qty
            # FIFO consume
            while sell_qty > 1e-18 and lots[sym]:
                lot_qty, lot_price = lots[sym][0]
                take = min(sell_qty, lot_qty)
                sell_qty -= take
                lot_qty -= take
                # Record realized only if this SELL happens today
                if start <= t.ts < end:
                    pnl = take * (t.price - lot_price)
                    fills.append(RealizedFill(
                        ts=t.ts, symbol=sym, qty=take, sell_price=t.price,
                        buy_cost=lot_price, pnl=pnl, tx=t.tx
                    ))
                # update lot
                if lot_qty <= 1e-18:
                    lots[sym].pop(0)
                else:
                    lots[sym][0][0] = lot_qty
            # if sell_qty remains but no lots => short; we’ll treat remaining cost as 0 for realized calc today
            if sell_qty > 1e-18 and start <= t.ts < end:
                pnl = sell_qty * (t.price - 0.0)
                fills.append(RealizedFill(
                    ts=t.ts, symbol=sym, qty=sell_qty, sell_price=t.price,
                    buy_cost=0.0, pnl=pnl, tx=t.tx
                ))

    # Summaries
    per_symbol: Dict[str, Dict[str, float]] = {}
    total_realized = 0.0
    total_fees = 0.0
    for rf in fills:
        d = per_symbol.setdefault(rf.symbol, {"realized": 0.0, "fees": 0.0, "qty_sold": 0.0})
        d["realized"] += rf.pnl
        d["qty_sold"] += rf.qty
        total_realized += rf.pnl

    # Collect fees for today's trades (both buy & sell fees reduce PnL)
    for t in todays_trades():
        if t.fee:
            per_symbol.setdefault(t.symbol, {"realized": 0.0, "fees": 0.0, "qty_sold": 0.0})
            per_symbol[t.symbol]["fees"] += t.fee
            total_fees += t.fee

    return RealizedSummary(
        window_start=start, window_end=end,
        per_symbol=per_symbol, fills=fills,
        total_realized=total_realized, total_fees=total_fees
    )

def realized_pnl_today() -> RealizedSummary:
    return _fifo_realized_today(load_trades())
