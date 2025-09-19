# core/rpc.py
"""
Minimal Cronos RPC helpers.
- Native CRO balance
- Generic ERC-20 token balance(s)
"""

import os
import logging
from decimal import Decimal
from typing import Dict, Any

import requests

RPC_URL = os.getenv("CRONOS_RPC_URL", "https://evm.cronos.org")

# Native CRO (gas token) has fixed decimals = 18
NATIVE_DECIMALS = 18


# ---------- Low-level JSON-RPC ----------

def _rpc_call(method: str, params: list[Any]) -> Any:
    try:
        resp = requests.post(
            RPC_URL,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error {data['error']}")
        return data.get("result")
    except Exception as e:
        logging.exception(f"RPC call {method} failed: {e}")
        return None


# ---------- Native CRO ----------

def get_native_balance(wallet_address: str) -> Decimal:
    """
    Return native CRO balance for the given address.
    """
    result = _rpc_call("eth_getBalance", [wallet_address, "latest"])
    if result is None:
        return Decimal("0")
    try:
        return Decimal(int(result, 16)) / Decimal(10**NATIVE_DECIMALS)
    except Exception as e:
        logging.warning(f"Failed to parse CRO balance {result}: {e}")
        return Decimal("0")


# ---------- ERC-20 Tokens ----------

# Minimal ERC-20 ABI fragments (for balanceOf & decimals & symbol)
ERC20_BALANCEOF = "0x70a08231"  # balanceOf(address)
ERC20_DECIMALS  = "0x313ce567"  # decimals()
ERC20_SYMBOL    = "0x95d89b41"  # symbol()


def _call_contract(to_address: str, data: str) -> Any:
    return _rpc_call("eth_call", [{"to": to_address, "data": data}, "latest"])


def _encode_address(addr: str) -> str:
    # Remove 0x and pad left
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


def get_erc20_balance(wallet_address: str, token_address: str) -> Decimal:
    """
    Return ERC-20 token balance for given wallet & token contract.
    """
    try:
        data = ERC20_BALANCEOF + _encode_address(wallet_address)[2:]
        raw = _call_contract(token_address, data)
        if raw is None:
            return Decimal("0")
        bal = int(raw, 16)

        # decimals
        dec_raw = _call_contract(token_address, ERC20_DECIMALS)
        decimals = int(dec_raw, 16) if dec_raw else 18

        return Decimal(bal) / Decimal(10**decimals)
    except Exception as e:
        logging.warning(f"ERC20 balance fetch failed: {e}")
        return Decimal("0")


def get_erc20_symbol(token_address: str) -> str:
    """
    Try to fetch ERC-20 symbol. If fail, return shortened address.
    """
    try:
        raw = _call_contract(token_address, ERC20_SYMBOL)
        if raw and len(raw) >= 66:
            # decode as ascii string padded
            hexdata = raw[130:] if raw.startswith("0x") else raw
            bytestr = bytes.fromhex(hexdata).rstrip(b"\x00")
            return bytestr.decode("utf-8")
    except Exception:
        pass
    return token_address[:6].upper()


def get_erc20_balances(wallet_address: str, token_map: Dict[str, str]) -> Dict[str, Decimal]:
    """
    Fetch balances for multiple ERC-20 tokens.
    token_map = { "SYMBOL": "0xTokenAddress", ... }
    Returns { "SYMBOL": Decimal(amount) }
    """
    balances: Dict[str, Decimal] = {}
    for sym, addr in token_map.items():
        amt = get_erc20_balance(wallet_address, addr)
        balances[sym] = amt
    return balances
