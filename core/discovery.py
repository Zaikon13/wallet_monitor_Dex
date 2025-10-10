# core/discovery.py
# Wallet token discovery via JSON-RPC with CHUNKED eth_getLogs + optional seeds.
from __future__ import annotations
import os, logging, requests, math
from decimal import Decimal
from typing import Optional, List, Dict, Any, Set, Tuple

logger = logging.getLogger("core.discovery")

# --- RPC endpoint
CRONOS_RPC_URL = (os.getenv("CRONOS_RPC_URL") or os.getenv("RPC_URL") or "").strip() \
                 or "https://cronos-evm-rpc.publicnode.com"
REQ_TIMEOUT = float(os.getenv("RPC_TIMEOUT", "15"))

# --- Config
DEFAULT_LOOKBACK = int(os.getenv("DISCOVERY_LOOKBACK", "500000"))   # πόσα blocks πίσω
CHUNK_SIZE       = int(os.getenv("DISCOVERY_CHUNK", "50000"))       # μέγεθος chunk
FROM_BLOCK_ENV   = os.getenv("DISCOVERY_FROM_BLOCK", "").strip()    # αν θέλεις fixed αρχή (hex ή int)
SEED_ADDRESSES   = [a.strip().lower() for a in os.getenv("SEED_TOKEN_ADDRESSES","").split(",") if a.strip()]

# Selectors
SEL_SYMBOL   = "0x95d89b41"
SEL_DECIMALS = "0x313ce567"
SEL_BALANCE  = "0x70a08231"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a6c4b5fbb"

# ---------- Low-level RPC ----------
def _rpc(payload: Dict[str, Any]) -> Any:
    r = requests.post(CRONOS_RPC_URL, json=payload, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]

def _eth_block_number() -> int:
    res = _rpc({"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]})
    return int(res, 16)

def _hex(n: int) -> str:
    return hex(n)

def _eth_get_logs_range(from_block_hex: str, to_block_hex: str, topics: List[Any]) -> List[Dict[str, Any]]:
    try:
        params = {"fromBlock": from_block_hex, "toBlock": to_block_hex, "topics": topics}
        return _rpc({"jsonrpc":"2.0","id":1,"method":"eth_getLogs","params":[params]})
    except Exception as e:
        logger.debug("eth_getLogs failed %s-%s: %s", from_block_hex, to_block_hex, e)
        return []

def _eth_call(to: str, data: str) -> Optional[str]:
    try:
        return _rpc({"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to": to, "data": data},"latest"]})
    except Exception:
        return None

# ---------- ERC-20 helpers ----------
def _addr_topic(wallet: str) -> str:
    return "0x" + wallet.lower().replace("0x","").rjust(64, "0")

def _decode_uint256(hexstr: str) -> int:
    if not hexstr: return 0
    return int(hexstr, 16)

def _decode_string(hexstr: str) -> Optional[str]:
    if not hexstr: return None
    try:
        raw = bytes.fromhex(hexstr.replace("0x",""))
        s = raw.rstrip(b"\x00").decode("utf-8", errors="ignore")
        if s: return s
    except Exception:
        pass
    try:
        data = hexstr[2:] if hexstr.startswith("0x") else hexstr
        if len(data) >= 128:
            strlen = int(data[64:128], 16)
            start  = 128; end = 128 + strlen*2
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
    w = wallet.lower().replace("0x","").rjust(64, "0")
    out = _eth_call(addr, SEL_BALANCE + w)
    return _decode_uint256(out) if out else 0

# ---------- Discovery ----------
def _uniq(seq: List[str]) -> List[str]:
    seen: Set[str] = set(); out: List[str] = []
    for x in seq:
        lx = x.lower()
        if lx and lx not in seen:
            seen.add(lx); out.append(lx)
    return out

def _scan_chunks(wallet: str, from_block: int, to_block: int) -> List[str]:
    """
    Χωρίζουμε το range σε κομμάτια και παίρνουμε logs για:
    topic0 = Transfer, topic1 = from=wallet  ΚΑΙ
    topic0 = Transfer, topic2 = to=wallet
    """
    addrs: List[str] = []
    total = max(to_block - from_block + 1, 0)
    chunks = max(math.ceil(total / CHUNK_SIZE), 1)

    topic_from = [_hex_str := _addr_topic(wallet)]
    topic_to   = topic_from  # ίδιο padded addr

    for i in range(chunks):
        start = from_block + i*CHUNK_SIZE
        end   = min(start + CHUNK_SIZE - 1, to_block)
        if start > end:
            break
        frm_hex = _hex(start); to_hex = _hex(end)

        # from=wallet
        logs1 = _eth_get_logs_range(frm_hex, to_hex, [TRANSFER_TOPIC, topic_from, None])
        # to=wallet
        logs2 = _eth_get_logs_range(frm_hex, to_hex, [TRANSFER_TOPIC, None, topic_to])

        for l in (logs1 + logs2):
            addr = l.get("address")
            if addr:
                addrs.append(addr)
    return _uniq(addrs)

def _seed_candidates() -> List[str]:
    return _uniq(SEED_ADDRESSES)

def _read_token(addr: str, wallet: str) -> Optional[Dict[str, Any]]:
    try:
        bal = _call_balance_of(addr, wallet)
        if bal <= 0:
            return None
        dec = _call_decimals(addr)
        sym = _call_symbol(addr) or addr[:6]
        amount = Decimal(bal) / (Decimal(10) ** Decimal(dec))
        return {"address": addr.lower(), "symbol": sym, "decimals": dec, "amount": amount}
    except Exception as e:
        logger.debug("read token failed %s: %s", addr, e)
        return None

def _resolve_from_block(latest_block: int) -> int:
    if FROM_BLOCK_ENV:
        try:
            return int(FROM_BLOCK_ENV, 16) if FROM_BLOCK_ENV.startswith("0x") else int(FROM_BLOCK_ENV)
        except Exception:
            pass
    # default: go back DEFAULT_LOOKBACK blocks
    start = max(latest_block - DEFAULT_LOOKBACK, 0)
    return start

def discover_tokens_for_wallet(wallet_address: str, lookback_blocks: int = None) -> List[Dict[str, Any]]:
    """
    Returns list of tokens (symbol,address,decimals,amount) with positive balance.
    1) chunked logs scan on Transfer() for from/to = wallet
    2) optional seeds via SEED_TOKEN_ADDRESSES
    """
    wallet = wallet_address.lower()

    # block range
    latest = _eth_block_number()
    start  = _resolve_from_block(latest)
    if lookback_blocks:
        start = max(latest - int(lookback_blocks), 0)

    # 1) chunked scan
    candidates = _scan_chunks(wallet, start, latest)

    # 2) seeds (force-check balances for these contracts)
    seeds = _seed_candidates()
    candidates.extend(seeds)

    tokens: List[Dict[str, Any]] = []
    for addr in _uniq(candidates):
        t = _read_token(addr, wallet)
        if t:
            tokens.append(t)
    return tokens
