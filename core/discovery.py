# core/discovery.py
# Wallet token discovery via JSON-RPC (chunked eth_getLogs) + Blockscout account.tokenlist + seeds από TOKENS.
from __future__ import annotations

import os
import re
import math
import logging
from decimal import Decimal
from typing import Optional, List, Dict, Any, Set

import requests

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

# Προτιμά LOG_* (υπάρχοντα), αλλιώς DISCOVERY_* αν υπάρχουν, αλλιώς defaults
LOOKBACK_BLOCKS = _int_env("LOG_SCAN_BLOCKS", "DISCOVERY_LOOKBACK", default=500000)
CHUNK_SIZE      = _int_env("LOG_SCAN_CHUNK",  "DISCOVERY_CHUNK",  default=4000)

# ---------- ABI selectors ----------
SEL_SYMBOL   = "0x95d89b41"
SEL_DECIMALS = "0x313ce567"
SEL_BALANCE  = "0x70a08231"

# ERC20 Transfer topic
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
    if not hexstr: 
        return 0
    return int(hexstr, 16)

def _decode_string(hexstr: str) -> Optional[str]:
    if not hexstr:
        return None
    # try fixed bytes-like
    try:
        raw = bytes.fromhex(hexstr.replace("0x",""))
        s = raw.rstrip(b"\x00").decode("utf-8", errors="ignore")
        if s:
            return s
    except Exception:
        pass
    # try dynamic ABI string
    try:
        data = hexstr[2:] if hexstr.startswith("0x") else hexstr
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

# ========== (A) Βοηθητικές για Blockscout account.tokenlist ==========
BLOCKSCOUT_BASE = os.getenv("CRONOS_EXPLORER_API", "https://cronos.org/explorer/api")

def _blockscout_tokenlist(address: str) -> List[Dict[str, Any]]:
    """
    Χτυπάει το Blockscout account API για να πάρει ΟΛΑ τα tokens που κατέχει το wallet.
    https://cronos.org/explorer/api?module=account&action=tokenlist&address=0x...
    Επιστρέφει λίστα dicts με τουλάχιστον: contractAddress, balance, symbol, decimals (όπου υπάρχουν).
    """
    try:
        r = requests.get(
            BLOCKSCOUT_BASE,
            params={"module":"account","action":"tokenlist","address":address},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        j = r.json() or {}
        res = j.get("result")
        if not res or not isinstance(res, list):
            return []
        return res
    except Exception as e:
        logger.debug("blockscout tokenlist failed: %s", e)
        return []

def _to_pos_int(x: Any) -> int:
    try:
        n = int(str(x))
        return n if n > 0 else 0
    except Exception:
        return 0

# ========== (B) Blockscout-first discovery helper ==========
def _discover_via_blockscout_tokenlist(wallet: str) -> List[Dict[str, Any]]:
    """
    Blockscout-first discovery: γυρνά tokens με balance>0 (symbol/decimals αν υπάρχουν).
    Αν κάποιο πεδίο λείπει, συμπληρώνουμε από on-chain calls (decimals/symbol/balanceOf).
    """
    rows = _blockscout_tokenlist(wallet)
    if not rows:
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        addr = (row.get("contractAddress") or row.get("contractaddress") or "").lower()
        if not addr.startswith("0x") or len(addr) != 42:
            continue

        raw_bal_s = row.get("balance")
        dec_s     = row.get("decimals")
        sym       = (row.get("symbol") or "").strip()

        # Συμπλήρωσε/διόρθωσε από on-chain όπου λείπει/χαλάει
        dec = _to_pos_int(dec_s) or _call_decimals(addr)
        try:
            raw_bal = int(str(raw_bal_s)) if raw_bal_s is not None else _call_balance_of(addr, wallet)
        except Exception:
            raw_bal = _call_balance_of(addr, wallet)

        if raw_bal <= 0:
            continue

        if not sym:
            sym = _call_symbol(addr) or addr[:6]

        amount = Decimal(raw_bal) / (Decimal(10) ** Decimal(dec))
        out.append({"address": addr, "symbol": sym, "decimals": dec, "amount": amount})
    return out

# ---------- Logs discovery (chunked) ----------
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
        dec = _call_decimals(addr)
        sym = _call_symbol(addr) or addr[:6]
        amount = Decimal(bal) / (Decimal(10) ** Decimal(dec))
        return {"address": addr.lower(), "symbol": sym, "decimals": dec, "amount": amount}
    except Exception as e:
        logger.debug("read token failed %s: %s", addr, e)
        return None

# ========== (C) Κύρια συνάρτηση: Blockscout πρώτα, μετά logs, μετά seeds ==========
def discover_tokens_for_wallet(wallet_address: str, lookback_blocks: int = None) -> List[Dict[str, Any]]:
    """
    Επιστρέφει tokens (symbol,address,decimals,amount) με balance>0.
    Προτεραιότητα:
      1) Blockscout account.tokenlist (πλήρης λίστα tokens που κατέχει το wallet)
      2) chunked logs (LOG_SCAN_BLOCKS / LOG_SCAN_CHUNK) για fallback/εμπλουτισμό
      3) seeds από TOKENS (αν έχεις ορίσει contracts)
    """
    wallet = wallet_address.lower()

    # 1) Blockscout tokenlist
    tokens = _discover_via_blockscout_tokenlist(wallet)
    found_addrs = {t["address"] for t in tokens}

    # 2) Fallback: logs scan για ό,τι δεν γύρισε το API
    latest = _eth_block_number()
    lookback = int(lookback_blocks) if lookback_blocks is not None else LOOKBACK_BLOCKS
    start = max(latest - lookback, 0)
    candidates = _scan_chunks(wallet, start, latest)

    # 3) Seeds από TOKENS env (αν υπάρχουν)
    try:
        seeds = _seed_contracts_from_tokens_env()
        if seeds:
            candidates.extend(seeds)
    except Exception:
        pass

    # Εμπλούτισε tokens με ό,τι βρέθηκε από logs/seeds που δεν υπήρχε ήδη
    for addr in _uniq(candidates):
        if addr in found_addrs:
            continue
        t = _read_token(addr, wallet)
        if t:
            tokens.append(t)
            found_addrs.add(addr)

    return tokens
