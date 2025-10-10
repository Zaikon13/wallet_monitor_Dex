# core/discovery.py
# Wallet token discovery via JSON-RPC (chunked eth_getLogs) + seeds από TOKENS env.
from __future__ import annotations
import os, re, logging, requests, math
from decimal import Decimal
from typing import Optional, List, Dict, Any, Set

logger = logging.getLogger("core.discovery")

# ---------- RPC endpoint ----------
CRONOS_RPC_URL = (os.getenv("CRONOS_RPC_URL") or os.getenv("RPC_URL") or "").strip() \
                 or "https://cronos-evm-rpc.publicnode.com"
REQ_TIMEOUT = float(os.getenv("RPC_TIMEOUT", "15"))

# ---------- Config από ΥΠΑΡΧΟΝΤΑ envs ----------
def _int_env(*names: str, default: int = 0) -> int:
    for n in names:
        v = os.getenv(n)
        if v is None or str(v).strip() == "":
            continue
        try:
            return int(v)
        except Exception:
            try:
                return int(v, 16) if str(v).startswith("0x") else int(v)
            except Exception:
                pass
    return default

LOOKBACK_BLOCKS = _int_env("LOG_SCAN_BLOCKS", "DISCOVERY_LOOKBACK", default=500000)
CHUNK_SIZE      = _int_env("LOG_SCAN_CHUNK",  "DISCOVERY_CHUNK",  default=4000)

# ---------- ABI selectors ----------
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
    # fixed bytes32-like
    try:
        raw = bytes.fromhex(hexstr.replace("0x",""))
        s = raw.rstrip(b"\x00").decode("utf-8", errors="ignore")
        if s: return s
    except Exception:
        pass
    # dynamic ABI string
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

# ---------- Seeds από TOKENS ----------
_ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")

def _seed_contracts_from_tokens_env() -> List[str]:
    """
    Διαβάζει το ήδη υπάρχον env TOKENS.
    Υποστηριζόμενες μορφές:
      - 'cronos/0xabc...,cronos/0xdef...,0xghi..., CRO/0x...'  (οτιδήποτε με 0x… θα παρθεί)
    """
    raw = os.getenv("TOKENS", "") or ""
    if not raw.strip():
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    out: List[str] = []
    for p in parts:
        m = _ADDR_RE.search(p)
        if m:
            out.append(m.group(0).lower())
    # de-dup
    seen: Set[str] = set()
    uniq = []
    for a in out:
        if a not in seen:
            seen.add(a); uniq.append(a)
    return uniq

# ---------- Blockscout metadata (symbol/decimals) ----------
# Base URL from official docs
BLOCKSCOUT_API = "https://cronos.org/explorer/api"  # token.getToken gives symbol/decimals, etc. (no balance)
# Docs: ?module=token&action=getToken&contractaddress=... (returns symbol/decimals) — we still use RPC for balanceOf
# Ref: https://cronos.org/explorer/api-docs
def _token_metadata_via_blockscout(addr: str) -> Dict[str, Any]:
    try:
        r = requests.get(BLOCKSCOUT_API, params={
            "module": "token",
            "action": "getToken",
            "contractaddress": addr
        }, timeout=10)
        if r.status_code == 200:
            j = r.json() or {}
            res = j.get("result") or {}
            sym = (res.get("symbol") or "").strip()
            dec = int(res.get("decimals") or 18)
            return {"symbol": sym or None, "decimals": dec}
    except Exception:
        pass
    return {}

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
    Chunked scan:
    topic0=Transfer, topic1=from=wallet
    topic0=Transfer, topic2=to=wallet
    """
    addrs: List[str] = []
    total = max(to_block - from_block + 1, 0)
    chunks = max(math.ceil(total / CHUNK_SIZE), 1)

    addr_topic = _addr_topic(wallet)
    t_from = [addr_topic]
    t_to   = [addr_topic]

    for i in range(chunks):
        start = from_block + i*CHUNK_SIZE
        end   = min(start + CHUNK_SIZE - 1, to_block)
        if start > end:
            break
        frm_hex = _hex(start); to_hex = _hex(end)

        logs1 = _eth_get_logs_range(frm_hex, to_hex, [TRANSFER_TOPIC, t_from, None])
        logs2 = _eth_get_logs_range(frm_hex, to_hex, [TRANSFER_TOPIC, None, t_to])

        for l in (logs1 + logs2):
            addr = l.get("address")
            if addr:
                addrs.append(addr)
    return _uniq(addrs)

def _read_token(addr: str, wallet: str) -> Optional[Dict[str, Any]]:
    try:
        bal = _call_balance_of(addr, wallet)
        if bal <= 0:
            return None
        meta = _token_metadata_via_blockscout(addr)  # σύμβολο/decimals αν υπάρχουν
        dec = meta.get("decimals") or _call_decimals(addr)
        sym = meta.get("symbol")   or _call_symbol(addr) or addr[:6]
        amount = Decimal(bal) / (Decimal(10) ** Decimal(dec))
        return {"address": addr.lower(), "symbol": sym, "decimals": dec, "amount": amount}
    except Exception as e:
        logger.debug("read token failed %s: %s", addr, e)
        return None

def discover_tokens_for_wallet(wallet_address: str, lookback_blocks: int = None) -> List[Dict[str, Any]]:
    """
    Returns tokens (symbol,address,decimals,amount) με balance>0.
    1) chunked logs (LOG_SCAN_BLOCKS / LOG_SCAN_CHUNK)
    2) seeds από TOKENS env (π.χ. 'cronos/0xabc...,0xdef...')
    """
    wallet = wallet_address.lower()
    latest = _eth_block_number()
    lookback = int(lookback_blocks) if lookback_blocks is not None else LOOKBACK_BLOCKS
    start = max(latest - lookback, 0)

    candidates = _scan_chunks(wallet, start, latest)

    # + seeds από TOKENS (αν έχεις βάλει contracts)
    seeds = _seed_contracts_from_tokens_env()
    candidates.extend(seeds)

    tokens: List[Dict[str, Any]] = []
    for addr in _uniq(candidates):
        t = _read_token(addr, wallet)
        if t:
            tokens.append(t)
    return tokens
