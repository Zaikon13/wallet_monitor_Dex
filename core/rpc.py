from __future__ import annotations
import os
import requests
from typing import Dict, List, Any

WEB3 = None
ERC20_ABI_MIN = [
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
    {"constant": True, "inputs": [{"name": "owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

# Environment
RPC_URL = os.getenv("CRONOS_RPC_URL", "") or os.getenv("RPC_URL", "")
WALLET_ADDR = (os.getenv("WALLET_ADDRESS") or "").lower()
ETHERSCAN_API = os.getenv("ETHERSCAN_API", "")

_sym_cache: Dict[str, str] = {}
_dec_cache: Dict[str, int] = {}

def rpc_init() -> bool:
    """
    Initialize Web3 provider once; return True if connected.
    """
    global WEB3
    if WEB3 is not None:
        try:
            return bool(WEB3.is_connected())
        except Exception:
            pass
    if not RPC_URL:
        return False
    try:
        from web3 import Web3
        WEB3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 15}))
        return bool(WEB3.is_connected())
    except Exception:
        WEB3 = None
        return False

def get_native_balance(addr: str) -> float:
    """
    Returns the CRO balance of the given address.
    """
    if not rpc_init():
        return 0.0
    try:
        wei = WEB3.eth.get_balance(WEB3.to_checksum_address(addr))
        return float(wei) / (10 ** 18)
    except Exception:
        return 0.0

def erc20_balance(contract: str, owner: str) -> float:
    """
    Returns the token balance of owner for the given ERC20 contract (scaled to token units).
    """
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
    """
    Returns the symbol for the given ERC20 contract.
    """
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
    """
    Returns (symbol, decimals) for the given ERC20 contract address.
    """
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
    """
    Scans recent blockchain logs for ERC20 Transfer events involving the owner address.
    Returns a set of token contract addresses.
    """
    if not rpc_init():
        return set()
    latest = None
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
                "topics": [transfer_topic, topic_addr]
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
                "topics": [transfer_topic, None, topic_addr]
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
    """
    Discover token contracts associated with the wallet and return list of those with balance > 0.
    Each item: {"token_addr": ..., "symbol": ..., "decimals": ..., "balance": ...}
    """
    tokens = []
    if not WALLET_ADDR:
        return tokens
    contracts = discover_token_contracts_by_logs(WALLET_ADDR, window_blocks, chunk)
    if not contracts and ETHERSCAN_API:
        try:
            params = {
                "chainid": 25,
                "module": "account",
                "action": "tokentx",
                "address": WALLET_ADDR,
                "startblock": 0,
                "endblock": 99999999,
                "page": 1,
                "offset": 1000,
                "sort": "desc",
                "apikey": ETHERSCAN_API
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
        bal = erc20_balance(addr, WALLET_ADDR)
        if bal > 1e-12:
            tokens.append({
                "token_addr": addr,
                "symbol": sym,
                "decimals": dec,
                "balance": bal
            })
    return tokens
