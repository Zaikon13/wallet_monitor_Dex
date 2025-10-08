# -*- coding: utf-8 -*-
"""
core/holdings.py — authoritative wallet snapshot for Telegram `/holdings`, `/show`, etc.

Notes
-----
- No network or schedule side-effects at import time.
- Depends on:
    * core.rpc.list_balances()                 -> iterable[{symbol, amount, address?}]
    * core.pricing.get_spot_usd(symbol, addr)  -> float | Decimal | None
  These are optional and guarded: on failure we degrade gracefully (price=0).
- CRO merge:
    * Any 'tCRO', 'TCRO', 'wcro receipt' is merged into 'CRO'.
- uPnL:
    * Best-effort attempt to fetch avg cost; if unavailable -> cost/uPnL set to 0.
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

# ---- Optional dependencies (guarded) -----------------------------------
try:
    from core import rpc  # type: ignore
except Exception:  # pragma: no cover
    rpc = None  # type: ignore

try:
    from core import pricing  # type: ignore
except Exception:  # pragma: no cover
    pricing = None  # type: ignore

# Cost-basis (optional)
try:
    from reports.ledger import get_avg_cost_usd  # type: ignore
except Exception:  # pragma: no cover
    def get_avg_cost_usd(symbol: str) -> Optional[Decimal]:
        return None

DEBUG_HOLDINGS = os.getenv("DEBUG_HOLDINGS", "0")
_DH_LEVEL = (
    int(DEBUG_HOLDINGS)
    if DEBUG_HOLDINGS.isdigit()
    else (
        2
        if DEBUG_HOLDINGS.lower() in ("2", "telegram", "tg")
        else (1 if DEBUG_HOLDINGS.lower() in ("1", "true", "yes", "on") else 0)
    )
)
_log = logging.getLogger("holdings")
_log.setLevel(logging.DEBUG)
if not _log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s holdings %(message)s"))
    _log.addHandler(_h)
try:
    # optional telegram for DEBUG_HOLDINGS=2
    from telegram.api import send_telegram as _send_tg  # type: ignore
except Exception:
    _send_tg = None


def _dbg(msg: str) -> None:
    if _DH_LEVEL >= 1:
        _log.debug(msg)
    if _DH_LEVEL >= 2 and _send_tg:
        try:
            _send_tg(f"🪵 [holdings-debug] {msg}")
        except Exception:
            pass


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


D = Decimal

CRO_ALIASES = {"TCRO", "tCRO", "tcro", "T-CRO", "t-cro", "WCRO-RECEIPT"}


def _norm_symbol(symbol: str) -> str:
    s = (symbol or "").strip()
    if s in CRO_ALIASES:
        return "CRO"
    # Normalize common wrappers
    if s.upper() in {"WCRO"}:
        return "CRO"
    return s or "?"


def _to_decimal(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x
    try:
        return D(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return D("0")


@dataclass
class AssetSnap:
    symbol: str
    amount: Decimal
    price_usd: Decimal
    value_usd: Decimal
    cost_usd: Decimal
    u_pnl_usd: Decimal
    u_pnl_pct: Decimal

    def to_row(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "amount": str(self.amount.normalize()),
            "price_usd": str(self.price_usd.quantize(D("0.00000001"))),
            "value_usd": str(self.value_usd.quantize(D("0.01"))),
            "cost_usd": str(self.cost_usd.quantize(D("0.01"))),
            "u_pnl_usd": str(self.u_pnl_usd.quantize(D("0.01"))),
            "u_pnl_pct": str(self.u_pnl_pct.quantize(D("0.01"))),
        }


def _discover_erc20_contracts_from_logs(blocks_back: int = 120_000) -> list[str]:
    """
    Use eth_getLogs over the last `blocks_back` blocks to find contracts that emitted Transfer(address,address,uint256)
    where our wallet is sender or receiver. Returns unique token contract addresses (checksum).
    Requires CRONOS_RPC_URL + WALLET_ADDRESS.
    """
    rpc_url = os.getenv("CRONOS_RPC_URL") or os.getenv("CRONOSRPCURL")
    wallet = os.getenv("WALLET_ADDRESS") or os.getenv("WALLETADDRESS")
    if not rpc_url or not wallet:
        return []
    try:
        from web3 import Web3  # type: ignore

        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        if not w3.is_connected():
            return []
        acct = Web3.to_checksum_address(wallet)
        acct_topic = "0x" + acct[2:].lower().rjust(64, "0")
        latest = w3.eth.block_number
        start = max(0, latest - int(blocks_back))
        topic0 = Web3.keccak(text="Transfer(address,address,uint256)").hex()

        params_common = {"fromBlock": hex(start), "toBlock": hex(latest), "topics": [topic0]}
        logs_to = []
        logs_from = []
        try:
            logs_to = w3.eth.get_logs({**params_common, "topics": [topic0, None, acct_topic]})  # type: ignore[assignment]
        except Exception:
            logs_to = []
        try:
            logs_from = w3.eth.get_logs({**params_common, "topics": [topic0, acct_topic, None]})  # type: ignore[assignment]
        except Exception:
            logs_from = []

        addrs: Set[str] = set()
        for lg in list(logs_to or []) + list(logs_from or []):
            try:
                contract_address = Web3.to_checksum_address(lg["address"])
                addrs.add(contract_address)
            except Exception:
                continue
        return sorted(addrs)
    except Exception:
        return []


def _fetch_erc20_balances_for_contracts(contracts: list[str]) -> list[Dict[str, Any]]:
    """
    For a list of token contract addresses, query balanceOf(wallet) and decimals(), and compose balance rows.
    Symbol resolution is best-effort using pricing.get_symbol or contract symbol() if available; falls back to address.
    """
    rpc_url = os.getenv("CRONOS_RPC_URL") or os.getenv("CRONOSRPCURL")
    wallet = os.getenv("WALLET_ADDRESS") or os.getenv("WALLETADDRESS")
    if not rpc_url or not wallet or not contracts:
        return []
    try:
        from web3 import Web3  # type: ignore
    except Exception:
        return []
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        if not w3.is_connected():
            return []
        acct = Web3.to_checksum_address(wallet)
        acct_bytes = bytes.fromhex(acct[2:].rjust(64, "0"))
        sel_balance = Web3.keccak(text="balanceOf(address)")[:4]
        sel_dec = Web3.keccak(text="decimals()")[:4]
        sel_sym = Web3.keccak(text="symbol()")[:4]
        out: list[Dict[str, Any]] = []
        for ca in contracts:
            try:
                addr = Web3.to_checksum_address(ca)
                data = sel_balance + acct_bytes
                raw = w3.eth.call({"to": addr, "data": "0x" + data.hex()})
                raw_bytes = bytes(raw) if raw else b""
                bal = int.from_bytes(raw_bytes, "big") if raw_bytes else 0
                if bal <= 0:
                    continue

                dec_raw = w3.eth.call({"to": addr, "data": "0x" + sel_dec.hex()}) or b""
                dec_bytes = bytes(dec_raw)
                decimals = int.from_bytes(dec_bytes, "big") if dec_bytes else 18
                if decimals < 0 or decimals > 36:
                    decimals = 18

                qty = Decimal(bal) / (Decimal(10) ** Decimal(decimals))
                if qty <= Decimal("0"):
                    continue

                symbol = None
                try:
                    sym_raw = w3.eth.call({"to": addr, "data": "0x" + sel_sym.hex()}) or b""
                    sym_bytes = bytes(sym_raw).rstrip(b"\x00")
                    if sym_bytes:
                        symbol_candidate = sym_bytes.decode("utf-8", "ignore").strip()
                        if symbol_candidate:
                            symbol = symbol_candidate
                except Exception:
                    pass

                if not symbol and pricing is not None and hasattr(pricing, "get_symbol_for_address"):
                    try:
                        symbol = pricing.get_symbol_for_address(addr)  # type: ignore[attr-defined]
                    except Exception:
                        symbol = None

                if not symbol:
                    symbol = addr[-6:]

                out.append({"symbol": symbol, "amount": qty, "address": addr})
            except Exception:
                continue
        return out
    except Exception:
        return []


# ---- Core snapshot ------------------------------------------------------
_dbg(
    f"boot fetch_balances ts={_ts()} rpc={'ok' if rpc is not None else 'missing'} pricing={'ok' if pricing is not None else 'missing'}"
)


def _fetch_balances() -> Iterable[Dict[str, Any]]:
    """
    Strategy:
      1) Live rpc.list_balances()
      2) If empty: rpc.rescan() and retry
      3) If still empty: native CRO via Web3
      4) If still empty: ERC20 discovery/fallbacks (if present)
      5) If still empty: local cache files
    """
    balances: list[Dict[str, Any]] = []

    # 1) direct list_balances()
    if rpc is not None and hasattr(rpc, "list_balances"):
        try:
            data = rpc.list_balances() or []
            _dbg(f"list_balances() -> len={len(data) if isinstance(data, list) else 'n/a'}")
            if isinstance(data, list) and data:
                balances = data
        except Exception as e:
            _dbg(f"list_balances() error={e!r}")

    # 2) rescan
    if (not balances) and (rpc is not None) and hasattr(rpc, "rescan"):
        try:
            _dbg("rescan() starting…")
            rpc.rescan()
            _dbg("rescan() done.")
        except Exception as e:
            _dbg(f"rescan() error={e!r}")
        try:
            data = rpc.list_balances() or []
            _dbg(
                "list_balances() after rescan -> len="
                f"{len(data) if isinstance(data, list) else 'n/a'}"
            )
            if isinstance(data, list) and data:
                balances = data
        except Exception as e:
            _dbg(f"list_balances() after rescan error={e!r}")

    # 3) native CRO
    if not balances:
        _dbg("native CRO fallback…")
        cro = _fallback_native_cro_balance()
        _dbg(f"native CRO result amount={str(cro.get('amount')) if cro else 'None'}")
        if cro is not None and _to_decimal(cro.get("amount")) > 0:
            balances = [cro]

    # 4) ERC20 discovery / fallback if present
    if not balances:
        if "_discover_erc20_contracts_from_logs" in globals():
            contracts: list[str] = []
            try:
                contracts = _discover_erc20_contracts_from_logs()  # type: ignore
            except Exception as e:
                _dbg(f"discover logs error={e!r}")
            else:
                _dbg(f"discover logs -> contracts={len(contracts)}")
            erc20s: list[Dict[str, Any]] = []
            if contracts:
                try:
                    erc20s = _fetch_erc20_balances_for_contracts(contracts)  # type: ignore
                except Exception as e:
                    _dbg(f"erc20 balances error={e!r}")
            _dbg(f"erc20 balances found={len(erc20s)}")
            if erc20s:
                balances = erc20s

    # 5) cache
    if not balances:
        try:
            import json
            import pathlib

            for c in ("./.cache/balances.json", "./data/balances.json", "./.ledger/balances.json"):
                p = pathlib.Path(c)
                if p.exists() and p.is_file():
                    data = json.loads(p.read_text(encoding="utf-8")) or []
                    _dbg(f"cache probe {c} -> len={len(data) if isinstance(data, list) else 'n/a'}")
                    if isinstance(data, list) and data:
                        balances = data
                        break
        except Exception as e:
            _dbg(f"cache read error={e!r}")

    _dbg(f"_fetch_balances() final len={len(balances)}")
    return balances or []


def _fallback_native_cro_balance() -> Optional[Dict[str, Any]]:
    """
    Read native CRO balance directly from RPC when higher-level adapter returns empty.
    Requires:
      - CRONOS_RPC_URL
      - WALLET_ADDRESS (hex)
    Returns a balance row or None if not available.
    """
    rpc_url = os.getenv("CRONOS_RPC_URL") or os.getenv("CRONOSRPCURL")
    wallet = os.getenv("WALLET_ADDRESS") or os.getenv("WALLETADDRESS")
    if not rpc_url or not wallet:
        return None
    try:
        from web3 import Web3  # type: ignore

        w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            return None
        wei = w3.eth.get_balance(Web3.to_checksum_address(wallet))
        cro = Decimal(wei) / Decimal(10**18)
        # Ignore dust-level noise; treat < 1e-12 as zero
        if cro.compare(Decimal("0.000000000001")) <= 0:
            return {"symbol": "CRO", "amount": Decimal("0"), "address": None}
        return {"symbol": "CRO", "amount": cro, "address": None}
    except Exception:
        return None


def _spot_usd(symbol: str, address: Optional[str]) -> Decimal:
    if pricing is None or not hasattr(pricing, "get_spot_usd"):
        return D("0")
    try:
        px = pricing.get_spot_usd(symbol=symbol, token_address=address)
        return _to_decimal(px)
    except Exception:
        return D("0")


def _avg_cost(symbol: str) -> Decimal:
    try:
        c = get_avg_cost_usd(symbol)  # type: ignore
        return _to_decimal(c)
    except Exception:
        return D("0")


def _merge_rows(rows: Iterable[Dict[str, Any]]) -> List[Tuple[str, Decimal, Optional[str]]]:
    """
    Merge raw balances by normalized symbol (CRO + tCRO -> CRO).
    Returns list of (symbol, total_amount, preferred_address).
    """
    agg: Dict[str, Tuple[Decimal, Optional[str]]] = {}
    for r in rows:
        sym = _norm_symbol(str(r.get("symbol") or r.get("token") or ""))
        amt = _to_decimal(r.get("amount") or r.get("qty") or r.get("balance"))
        addr = r.get("address") or r.get("token_address") or r.get("contract")
        if sym not in agg:
            agg[sym] = (amt, addr)
        else:
            cur_amt, cur_addr = agg[sym]
            agg[sym] = (cur_amt + amt, cur_addr or addr)
    # drop zero rows
    out: List[Tuple[str, Decimal, Optional[str]]] = [
        (s, a, ad) for s, (a, ad) in agg.items() if a != 0
    ]
    # sort by symbol for deterministic output
    out.sort(key=lambda t: t[0])
    return out


def get_wallet_snapshot(base_ccy: str = "USD", limit: int = 9999) -> Dict[str, Any]:
    """
    Public API used by telegram commands.

    Returns:
        {
          "assets": [ {symbol, amount, price_usd, value_usd, cost_usd, u_pnl_usd, u_pnl_pct}, ... ],
          "totals": { value_usd, cost_usd, u_pnl_usd, u_pnl_pct }
        }
    """
    raw = list(_fetch_balances())
    merged = _merge_rows(raw)

    snaps: List[AssetSnap] = []
    total_val = D("0")
    total_cost = D("0")

    for sym, amt, addr in merged:
        px = _spot_usd(sym, addr)
        val = (amt * px).quantize(D("0.00000001"))
        cost_per_unit = _avg_cost(sym)
        cost = (cost_per_unit * amt) if cost_per_unit else D("0")
        upnl = (val - cost)
        upct = (upnl / cost * 100) if cost > 0 else D("0")
        snaps.append(
            AssetSnap(
                symbol=sym,
                amount=amt,
                price_usd=px,
                value_usd=val,
                cost_usd=cost,
                u_pnl_usd=upnl,
                u_pnl_pct=upct,
            )
        )
        total_val += val
        total_cost += cost

    # sort by value desc
    snaps.sort(key=lambda s: s.value_usd, reverse=True)
    if limit and limit > 0:
        snaps = snaps[:limit]

    total_upnl = total_val - total_cost
    total_upct = (total_upnl / total_cost * 100) if total_cost > 0 else D("0")

    return {
        "assets": [s.to_row() for s in snaps],
        "totals": {
            "value_usd": str(total_val.quantize(D("0.01"))),
            "cost_usd": str(total_cost.quantize(D("0.01"))),
            "u_pnl_usd": str(total_upnl.quantize(D("0.01"))),
            "u_pnl_pct": str(total_upct.quantize(D("0.01"))),
        },
    }


# Attach a light debug wrapper so we can emit totals when enabled
def get_wallet_snapshot_debug(base_ccy: str = "USD", limit: int = 9999) -> Dict[str, Any]:
    snap = get_wallet_snapshot(base_ccy=base_ccy, limit=limit)
    if _DH_LEVEL >= 1:
        assets = snap.get("assets", [])
        totals = snap.get("totals", {})
        _dbg(f"snapshot assets={len(assets)} totals_val={totals.get('value_usd')}")
    return snap


# Back-compat alias some projects used
wallet_snapshot = get_wallet_snapshot
