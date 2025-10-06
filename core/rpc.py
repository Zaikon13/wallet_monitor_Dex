from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping

import requests

WEB3 = None
ERC20_ABI_MIN = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

DEFAULT_CONFIG: Dict[str, str] = {
    "rpc_url": "",
    "wallet_address": "",
    "etherscan_api": "",
}

_RPC_CONFIG: Dict[str, str] = dict(DEFAULT_CONFIG)
_sym_cache: Dict[str, str] = {}
_dec_cache: Dict[str, int] = {}


def get_rpc_config(env: Mapping[str, str] = os.environ) -> Dict[str, str]:
    """Read RPC-related environment variables and return a sanitized config."""
    rpc_url = (env.get("CRONOS_RPC_URL") or env.get("RPC_URL") or "").strip()
    wallet_address = (env.get("WALLET_ADDRESS") or "").lower().strip()
    etherscan_api = (env.get("ETHERSCAN_API") or "").strip()
    return {
        "rpc_url": rpc_url,
        "wallet_address": wallet_address,
        "etherscan_api": etherscan_api,
    }


def configure_rpc(config: Dict[str, Any] | None = None) -> Dict[str, str]:
    """Apply a new RPC configuration. Returns the active config."""
    global _RPC_CONFIG, WEB3
    if config is None:
        config = get_rpc_config()
    applied = dict(DEFAULT_CONFIG)
    for key in applied:
        value = config.get(key) if config else None
        if value is None:
            continue
        text = str(value).strip()
        if key == "wallet_address":
            applied[key] = text.lower()
        else:
            applied[key] = text
    if applied["rpc_url"] != _RPC_CONFIG.get("rpc_url"):
        WEB3 = None  # reset connection to apply new endpoint
    _RPC_CONFIG = applied
    return dict(_RPC_CONFIG)


def rpc_init() -> bool:
    """Initialize Web3 provider once; return True if connected."""
    global WEB3
    if WEB3 is not None:
        try:
            return bool(WEB3.is_connected())
        except Exception:
            pass

    rpc_url = _RPC_CONFIG.get("rpc_url", "")
    if not rpc_url:
        return False
    try:
        from web3 import Web3

        WEB3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        return bool(WEB3.is_connected())
    except Exception:
        WEB3 = None
        return False


def get_native_balance(addr: str) -> float:
    """Return the CRO balance of the given address."""
    if not rpc_init():
        return 0.0
    try:
        wei = WEB3.eth.get_balance(WEB3.to_checksum_address(addr))
        return float(wei) / (10 ** 18)
    except Exception:
        return 0.0


def erc20_balance(contract: str, owner: str) -> float:
    """Return the ERC-20 token balance of the owner for the given contract."""
    if not rpc_init():
        return 0.0
    try:
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
    """Return the symbol for the given ERC-20 contract."""
    if contract in _sym_cache:
        return _sym_cache[contract]
    if not rpc_init():
        _sym_cache[contract] = contract[:8].upper()
        return _sym_cache[contract]
    try:
        c = WEB3.eth.contract(address=WEB3.to_checksum_address(contract), abi=ERC20_ABI_MIN)
        sym = c.functions.symbol().call()
        if isinstance(sym, bytes):
            sym = sym.decode("utf-8", "ignore").strip()
        _sym_cache[contract] = sym or contract[:8].upper()
        return _sym_cache[contract]
    except Exception:
        _sym_cache[contract] = contract[:8].upper()
        return _sym_cache[contract]


def get_symbol_decimals(contract: str) -> tuple[str, int]:
    """Return (symbol, decimals) for the given ERC-20 contract address."""
    if contract in _sym_cache and contract in _dec_cache:
        return (_sym_cache[contract], _dec_cache[contract])
    if not rpc_init():
        _sym_cache[contract] = contract[:8].upper()
        _dec_cache[contract] = 18
        return (_sym_cache[contract], _dec_cache[contract])
    try:
        c = WEB3.eth.contract(address=WEB3.to_checksum_address(contract), abi=ERC20_ABI_MIN)
        sym = c.functions.symbol().call()
        dec = int(c.functions.decimals().call())
        if isinstance(sym, bytes):
            sym = sym.decode("utf-8", "ignore").strip()
        _sym_cache[contract] = sym or contract[:8].upper()
        _dec_cache[contract] = dec if dec is not None else 18
    except Exception:
        _sym_cache[contract] = contract[:8].upper()
        _dec_cache[contract] = 18
    return (_sym_cache[contract], _dec_cache[contract])


def discover_token_contracts_by_logs(owner: str, blocks_back: int, chunk: int) -> set[str]:
    """Scan recent blockchain logs for ERC-20 Transfer events involving the owner address."""
    if not rpc_init():
        return set()
    try:
        latest = WEB3.eth.block_number
    except Exception:
        return set()
    if latest is None:
        return set()
    start_block = max(0, latest - max(1, blocks_back))
    found: set[str] = set()
    owner_hex = owner.lower().replace("0x", "")
    topic_addr = "0x" + owner_hex.rjust(64, "0")
    transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    current_block = start_block
    while current_block <= latest:
        end_block = min(latest, current_block + chunk - 1)
        try:
            logs = WEB3.eth.get_logs({
                "fromBlock": current_block,
                "toBlock": end_block,
                "topics": [transfer_topic, topic_addr],
            })
            for log in logs:
                addr = (log.get("address") or "").lower()
                if addr.startswith("0x"):
                    found.add(addr)
        except Exception:
            pass
        try:
            logs = WEB3.eth.get_logs({
                "fromBlock": current_block,
                "toBlock": end_block,
                "topics": [transfer_topic, None, topic_addr],
            })
            for log in logs:
                addr = (log.get("address") or "").lower()
                if addr.startswith("0x"):
                    found.add(addr)
        except Exception:
            pass
        current_block = end_block + 1
    return found


def discover_wallet_tokens(window_blocks: int = 120000, chunk: int = 5000) -> List[Dict[str, Any]]:
    """Discover wallet tokens with positive balance via RPC and optional Etherscan fallback."""
    tokens: List[Dict[str, Any]] = []
    wallet = _RPC_CONFIG.get("wallet_address", "")
    if not wallet:
        return tokens

    contracts = discover_token_contracts_by_logs(wallet, window_blocks, chunk)
    etherscan_api = _RPC_CONFIG.get("etherscan_api", "")

    if not contracts and etherscan_api:
        try:
            params = {
                "chainid": 25,
                "module": "account",
                "action": "tokentx",
                "address": wallet,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 1000,
                "sort": "desc",
                "apikey": etherscan_api,
            }
            resp = requests.get("https://api.etherscan.io/v2/api", params=params, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if str(data.get("status", "")).strip() == "1":
                    for tx in data.get("result", []):
                        ca = (tx.get("contractAddress") or "").lower()
                        if ca.startswith("0x"):
                            contracts.add(ca)
        except Exception:
            pass

    if not contracts:
        return tokens

    for addr in sorted(contracts):
        if not addr.startswith("0x"):
            continue
        sym, dec = get_symbol_decimals(addr)
        bal = erc20_balance(addr, wallet)
        if bal > 1e-12:
            tokens.append({
                "token_addr": addr,
                "symbol": sym,
                "decimals": dec,
                "balance": bal,
            })
    return tokens
