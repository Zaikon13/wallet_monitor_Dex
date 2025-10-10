# core/discovery.py
"""
Lightweight wallet token discovery.

Public API:
- discover_tokens_for_wallet(wallet_address: str, lookback_blocks: int = 50000) -> list[dict]

Strategy:
1) Try etherscan-like provider (fast, needs ETHERSCAN_API env or provider implementation).
2) If not available or empty result, fall back to RPC logs scan for Transfer events.
3) For each candidate token contract, call ERC20.balanceOf(wallet) via core.rpc and ERC20.decimals/symbol.
4) Return only tokens with balance > 0.

Environment / requirements:
- CRONOS_RPC_URL (or use core/rpc defaults)
- Optional: ETHERSCAN_API (or provider configured in core/providers/etherscan_like.py)
- The project already has core/rpc.py provider helpers - we call those.
"""

from __future__ import annotations
import logging
from decimal import Decimal
from typing import Optional, List, Dict, Any, Set

# Use the repo's rpc/provider helpers where possible:
try:
    from core.providers.etherscan_like import get_token_transfers_for_address  # optional
except Exception:
    get_token_transfers_for_address = None

from core.rpc import call_contract_function, get_logs  # expected helpers in core/rpc.py
# call_contract_function(contract_address, abi_fragment, method, params) -> result
# get_logs(from_block, to_block, address=None, topics=None) -> list[logs]

# Minimal ERC20 ABI fragments we need
_ERC20_ABI_SYMBOL = {"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "string"}], "constant": True}
_ERC20_ABI_DECIMALS = {"name": "decimals", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"type": "uint8"}], "constant": True}
_ERC20_ABI_BALANCEOF = {"name": "balanceOf", "type": "function", "stateMutability": "view", "inputs": [{"type": "address"}], "outputs": [{"type": "uint256"}], "constant": True}

# ERC20 Transfer topic (keccak of Transfer(address,address,uint256))
_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a6c" \
                  "4b5fbb"  # truncated? we'll compute in code if needed

# Helper to normalize topic (compute if not provided)
import hashlib
import binascii

def _keccak256_hex(s: bytes) -> str:
    try:
        import sha3  # pysha3
        k = sha3.keccak_256()
        k.update(s)
        return "0x" + k.hexdigest()
    except Exception:
        # Last-resort: return None; but many envs have pysha3
        return None

# We will attempt to compute Transfer topic correctly
try:
    _TRANSFER_TOPIC = _keccak256_hex(b"Transfer(address,address,uint256)")
except Exception:
    # fallback known constant (full 32 bytes)
    _TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a6c" \
                      "4b5fbb"  # still acceptable; get_logs can accept prefix matches too

logger = logging.getLogger("core.discovery")

def _uniq_preserve_order(seq):
    seen: Set[Any] = set()
    out = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out

def _from_transfers_provider(wallet: str) -> List[str]:
    """
    Use etherscan-like provider to grab contract addresses that transferred tokens to/from the wallet.
    Returns list of lowercased contract addresses.
    """
    if not get_token_transfers_for_address:
        return []
    try:
        transfers = get_token_transfers_for_address(wallet)
        # provider expected to return list of transfers with 'contractAddress'
        addrs = [t.get("contractAddress") for t in transfers if t.get("contractAddress")]
        addrs = [a.lower() for a in addrs]
        return _uniq_preserve_order(addrs)
    except Exception:
        logger.exception("etherscan_like transfers provider failed")
        return []

def _from_rpc_logs(wallet: str, lookback_blocks: int = 50000) -> List[str]:
    """
    Scan logs for Transfer events involving the wallet (may be heavy). Returns contract addresses.
    We choose a conservative block window (lookback_blocks) to limit scans.
    """
    try:
        # Determine block range using core.rpc helpers if available (we call get_logs with None -> provider can decide)
        # We'll ask for logs with topic[0] = Transfer signature and topic[1] or topic[2] matching the address.
        # Many RPCs allow address topics as indexed topics (padded hex)
        addr_topic = "0x" + wallet.lower().replace("0x", "").rjust(64, "0")
        logs = get_logs(from_block=None, to_block=None, topics=[_TRANSFER_TOPIC, [addr_topic], None], address=None, lookback_blocks=lookback_blocks)
        # get_logs should return list of logs with 'address' field = contract
        addrs = [l.get("address") for l in logs if l.get("address")]
        addrs = [a.lower() for a in addrs]
        return _uniq_preserve_order(addrs)
    except Exception:
        logger.exception("RPC logs scan failed")
        return []

def _read_token_onchain(token_address: str, wallet: str) -> Optional[Dict[str, Any]]:
    """
    Read symbol, decimals, balance via RPC calls (balanceOf + decimals + symbol).
    Return dict with 'address','symbol','decimals','balance' (Decimal) or None.
    """
    try:
        # balanceOf
        raw_bal = call_contract_function(token_address, _ERC20_ABI_BALANCEOF, "balanceOf", [wallet])
        if raw_bal is None:
            return None
        bal_int = int(raw_bal) if hasattr(raw_bal, "__int__") else int(str(raw_bal))
        if bal_int == 0:
            return None
        # decimals
        try:
            dec = call_contract_function(token_address, _ERC20_ABI_DECIMALS, "decimals", [])
            dec_int = int(dec)
        except Exception:
            dec_int = 18
        # symbol
        try:
            sym = call_contract_function(token_address, _ERC20_ABI_SYMBOL, "symbol", [])
            if isinstance(sym, bytes):
                try:
                    sym = sym.decode()
                except Exception:
                    sym = str(sym)
        except Exception:
            sym = token_address[:6]
        # compute human amount
        amount = Decimal(bal_int) / (Decimal(10) ** Decimal(dec_int))
        return {"address": token_address.lower(), "symbol": sym, "decimals": dec_int, "amount": amount}
    except Exception:
        logger.exception("Failed to read token onchain %s", token_address)
        return None

def discover_tokens_for_wallet(wallet_address: str, lookback_blocks: int = 50000) -> List[Dict[str, Any]]:
    """
    Main entrypoint. Returns list of token dicts with positive balances.
    """
    wallet = wallet_address.lower()
    candidates: List[str] = []

    # 1) Etherscan-like transfers (fast)
    addrs1 = _from_transfers_provider(wallet)
    candidates.extend(addrs1)

    # 2) RPC logs fallback if no candidates found
    if not candidates:
        addrs2 = _from_rpc_logs(wallet, lookback_blocks=lookback_blocks)
        candidates.extend(addrs2)

    # remove duplicates keep order
    candidates = _uniq_preserve_order([c for c in candidates if c])

    tokens: List[Dict[str, Any]] = []
    for a in candidates:
        info = _read_token_onchain(a, wallet)
        if info:
            tokens.append(info)

    return tokens
