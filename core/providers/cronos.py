# core/providers/cronos.py
from __future__ import annotations
import os, logging
from decimal import Decimal
from typing import Any, Optional

try:
    import requests
except Exception:
    requests = None  # type: ignore

RPC_URL = os.getenv("CRONOS_RPC_URL", "https://cronos-evm-rpc.publicnode.com").strip()
NATIVE_DECIMALS = 18

def _rpc_call(method: str, params: list[Any]) -> Optional[dict]:
    if requests is None:
        logging.warning("requests not available for RPC")
        return None
    try:
        r = requests.post(
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            logging.warning("RPC error on %s: %s", method, data["error"])
            return None
        return data
    except Exception as e:
        logging.warning("RPC %s failed: %s", method, e)
        return None

def get_native_balance(address: str) -> Decimal:
    """
    Returns CRO balance as Decimal(CRO), never raises. 0 on error.
    """
    try:
        rs = _rpc_call("eth_getBalance", [address, "latest"])
        if not rs: return Decimal("0")
        hexwei = (rs.get("result") or "0x0").lower()
        wei = int(hexwei, 16)
        return Decimal(wei) / (Decimal(10) ** NATIVE_DECIMALS)
    except Exception:
        return Decimal("0")

def erc20_balance_of(contract: str, address: str) -> int:
    """
    balanceOf(address) → uint256 (raw wei). Returns 0 on error.
    """
    try:
        # function selector keccak("balanceOf(address)")[:4] = 0x70a08231
        selector = "0x70a08231000000000000000000000000" + address.lower().replace("0x", "")
        rs = _rpc_call("eth_call", [{"to": contract, "data": selector}, "latest"])
        if not rs: return 0
        raw = (rs.get("result") or "0x0")
        return int(raw, 16)
    except Exception:
        return 0
