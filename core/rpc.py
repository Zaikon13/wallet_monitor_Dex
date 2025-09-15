# core/rpc.py
# Minimal Cronos RPC helpers (native CRO + optional ERC20 balances)
from __future__ import annotations
import os
from typing import Optional, Dict

WEB3 = None

ERC20_ABI_MIN = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]


def rpc_init() -> bool:
    """Initialize Web3 provider once; return connected flag."""
    global WEB3
    if WEB3 is not None:
        try:
            return bool(WEB3.is_connected())
        except Exception:
            pass
    url = os.getenv("CRONOS_RPC_URL", "")
    if not url:
        return False
    try:
        from web3 import Web3
        WEB3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
        return bool(WEB3.is_connected())
    except Exception:
        WEB3 = None
        return False


def get_native_balance(addr: str) -> float:
    try:
        if not rpc_init():
            return 0.0
        wei = WEB3.eth.get_balance(addr)
        return float(wei) / (10 ** 18)
    except Exception:
        return 0.0


_sym_cache: Dict[str, str] = {}
_dec_cache: Dict[str, int] = {}


def erc20_balance(contract: str, owner: str) -> float:
    try:
        if not rpc_init():
            return 0.0
        c = WEB3.eth.contract(address=WEB3.to_checksum_address(contract), abi=ERC20_ABI_MIN)
        bal = c.functions.balanceOf(WEB3.to_checksum_address(owner)).call()
        if contract not in _dec_cache:
            try:
                _dec_cache[contract] = int(c.functions.decimals().call())
            except Exception:
                _dec_cache[contract] = 18
        return float(bal) / (10 ** _dec_cache[contract])
    except Exception:
        return 0.0


def erc20_symbol(contract: str) -> str:
    if contract in _sym_cache:
        return _sym_cache[contract]
    try:
        if not rpc_init():
            return contract[:8].upper()
        c = WEB3.eth.contract(address=WEB3.to_checksum_address(contract), abi=ERC20_ABI_MIN)
        sym = c.functions.symbol().call()
        if isinstance(sym, bytes):
            sym = sym.decode("utf-8", "ignore").strip()
        _sym_cache[contract] = sym or contract[:8].upper()
        return _sym_cache[contract]
    except Exception:
        _sym_cache[contract] = contract[:8].upper()
        return _sym_cache[contract]
