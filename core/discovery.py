# core/discovery.py
# Lightweight wallet token discovery via direct JSON-RPC (no core.rpc deps)
from __future__ import annotations
import os, logging, requests, time
from decimal import Decimal
from typing import Optional, List, Dict, Any, Set

logger = logging.getLogger("core.discovery")

# --- Config / RPC endpoint
CRONOS_RPC_URL = (os.getenv("CRONOS_RPC_URL") or os.getenv("RPC_URL") or "").strip() \
                 or "https://cronos-evm-rpc.publicnode.com"
_REQ_TIMEOUT = float(os.getenv("RPC_TIMEOUT", "12"))

# --- ERC20 minimal ABI encodings (pre-encoded selectors)
# keccak256("symbol()")[:4], "decimals()", "balanceOf(address)"
SEL_SYMBOL   = "0x95d89b41"
SEL_DECIMALS = "0x313ce567"
SEL_BALANCE  = "0x70a08231"

# ERC20 Transfer topic = keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a6c4b5fbb"

def _rpc(payload: Dict[str, Any]) -> Any:
    r = requests.post(CRONOS_RPC_URL, json=payload, timeout=_REQ_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]

def _eth_call(to: str, data: str) -> Optional[str]:
    try:
        return _rpc({
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"]
        })
    except Exception as e:
        logger.debug("eth_call failed %s %s: %s", to, data[:10], e)
        return None

def _eth_get_logs(from_block: Optional[str], to_block: Optional[str],
                  address: Optional[str], topics: List[Any]) -> List[Dict[str, Any]]:
    try:
        params = {"fromBlock": from_block or "0x0", "toBlock": to_block or "latest"}
        if address: params["address"] = address
        if topics:  params["topics"]  = topics
        return _rpc({"jsonrpc":"2.0","id":1,"method":"eth_getLogs","params":[params]})
    except Exception as e:
        logger.debug("eth_getLogs failed: %s", e)
        return []

def _addr_topic(wallet: str) -> str:
    return "0x" + wallet.lower().replace("0x", "").rjust(64, "0")

def _decode_uint256(hexstr: str) -> int:
    if not hexstr: return 0
    return int(hexstr, 16)

def _decode_string(hexstr: str) -> Optional[str]:
    if not hexstr: return None
    # try raw bytes32(…)
    try:
        raw = bytes.fromhex(hexstr.replace("0x",""))
        s = raw.rstrip(b"\x00").decode("utf-8", errors="ignore")
        if s: return s
    except Exception:
        pass
    # try ABI-encoded dynamic string (offset/len)
    try:
        data = hexstr[2:] if hexstr.startswith("0x") else hexstr
        # skip first 64 (offset), read length next 64
        if len(data) >= 128:
            strlen = int(data[64:128], 16)
            start  = 128
            end    = 128 + strlen*2
            raw    = bytes.fromhex(data[start:end])
            return raw.decode("utf-8", errors="ignore") or None
    except Exception:
        pass
    return None

def _call_symbol(addr: str) -> Optional[str]:
    out = _eth_call(addr, SEL_SYMBOL)
    return _decode_string(out) if out else None

def _call_decimals(addr: str) -> int:
    out = _eth_call(addr, SEL_DECIMALS)
    try:
        return int(out, 16) if out else 18
    except Exception:
        return 18

def _call_balance_of(addr: str, wallet: str) -> int:
    # balanceOf(address) = selector + 12-bytes pad + 20-bytes address
    w = wallet.lower().replace("0x","").rjust(64, "0")
    data = SEL_BALANCE + w
    out = _eth_call(addr, data)
    return _decode_uint256(out) if out else 0

def _uniq(seq: List[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for x in seq:
        lx = x.lower()
        if lx not in seen:
            seen.add(lx); out.append(lx)
    return out

def _from_etherscan_like(wallet: str) -> List[str]:
    """
    Optional: αν έχεις core/providers/etherscan_like.get_token_transfers_for_address.
    Αν δεν υπάρχει/σκάσει, επιστρέφει [] και θα πάμε σε RPC logs.
    """
    try:
        from core.providers.etherscan_like import get_token_transfers_for_address
    except Exception:
        return []
    try:
        transfers = get_token_transfers_for_address(wallet)
        addrs = [t.get("contractAddress","") for t in (transfers or []) if t.get("contractAddress")]
        return _uniq([a for a in addrs if a])
    except Exception as e:
        logger.debug("etherscan_like provider failed: %s", e)
        return []

def _from_rpc_logs(wallet: str, lookback_blocks: int) -> List[str]:
    # περιορισμένο window για ταχύτητα· μπορείς να το αυξήσεις από ENV DISCOVERY_LOOKBACK
    try:
        # Χτυπάμε topic0=Transfer, topic1=from=wallet  (πιάνει εισροές/εκροές με variations)
        logs1 = _eth_get_logs(None, None, None, [TRANSFER_TOPIC, [_addr_topic(wallet)], None])
        # και topic2=to=wallet
        logs2 = _eth_get_logs(None, None, None, [TRANSFER_TOPIC, None, [_addr_topic(wallet)]])
        addrs = [l.get("address","") for l in (logs1+logs2) if l.get("address")]
        return _uniq([a for a in addrs if a])
    except Exception as e:
        logger.debug("rpc logs scan failed: %s", e)
        return []

def _read_token(addr: str, wallet: str) -> Optional[Dict[str, Any]]:
    try:
        bal = _call_balance_of(addr, wallet)
        if bal == 0:
            return None
        dec = _call_decimals(addr)
        sym = _call_symbol(addr) or addr[:6]
        amount = Decimal(bal) / (Decimal(10) ** Decimal(dec))
        return {"address": addr.lower(), "symbol": sym, "decimals": dec, "amount": amount}
    except Exception as e:
        logger.debug("read token failed %s: %s", addr, e)
        return None

def discover_tokens_for_wallet(wallet_address: str, lookback_blocks: int = None) -> List[Dict[str, Any]]:
    """
    Returns list of tokens (symbol,address,decimals,amount) with positive balance.
    Tries etherscan-like first, then falls back to direct RPC logs.
    """
    wallet = wallet_address.lower()
    lookback_blocks = lookback_blocks or int(os.getenv("DISCOVERY_LOOKBACK", "50000"))

    candidates: List[str] = []
    a1 = _from_etherscan_like(wallet)
    if a1: candidates.extend(a1)
    if not candidates:
        a2 = _from_rpc_logs(wallet, lookback_blocks)
        candidates.extend(a2)

    tokens: List[Dict[str, Any]] = []
    for addr in _uniq([a for a in candidates if a]):
        t = _read_token(addr, wallet)
        if t:
            tokens.append(t)
    return tokens
