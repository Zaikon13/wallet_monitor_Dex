# realtime/monitor.py
from __future__ import annotations
import os
import asyncio
import logging
import random
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal
from datetime import datetime, timezone

from web3 import Web3
from web3.types import LogReceipt

# ===================== ENV / Defaults =====================
WALLET = (os.getenv("WALLET_ADDRESS") or "").strip()

# Endpoints pool (WSS + HTTPS). HTTPS τιμά και το CRONOS_RPC_URL ως alias.
WSS_URLS = [u.strip() for u in (os.getenv("CRONOS_WSS_URL", "").split(",")) if u.strip()]
_https_env = os.getenv("CRONOS_HTTPS_URL") or os.getenv("CRONOS_RPC_URL") or "https://cronos-evm-rpc.publicnode.com"
HTTPS_URLS = [u.strip() for u in _https_env.split(",") if u.strip()]

CONFIRMATIONS = int(os.getenv("RT_CONFIRMS", "0"))                # 0 = instant
POLL_INTERVAL = float(os.getenv("RT_POLL_SEC", "0.8"))            # base poll
LEDGER_PATH = os.getenv("LEDGER_CSV", "data/ledger.csv")

# Backfill
BACKFILL_BLOCKS   = int(os.getenv("RT_BACKFILL_BLOCKS", "5000"))
BACKFILL_NOTIFY   = (os.getenv("RT_BACKFILL_NOTIFY", "0").strip() in ("1","true","yes"))
BACKFILL_CHUNK    = int(os.getenv("RT_BACKFILL_CHUNK", "500"))    # blocks per getLogs
BACKOFF_MAX_SEC   = float(os.getenv("RT_BACKOFF_MAX_SEC", "20"))  # cap backoff time

# Routers (extend as needed)
KNOWN_ROUTERS: Dict[str, str] = {
    "0x145863eb42cf62847a6ca784e6416c1682b1b2ae": "VVS Router",
    "0xe0137ee596c35bf7adedad1e2fd25da595d1e05b": "VVS Router (alt)",
    "0x22d710931f01c1681ca1570ff016ed42eb7b7c2a": "MMF Router",
    "0x76c930c6a2c2d7ee1e2fcfef05b0bb2e6902a84a": "Odos Router",
    "0xdef1abe32c034e558cdd535791643c58a13acc10": "Router (agg)",
}
KNOWN_ROUTERS = {k.lower(): v for k, v in KNOWN_ROUTERS.items()}

# Router selectors
SWAP_SELECTORS = {
    "0x38ed1739","0x18cbafe5","0x7ff36ab5","0x8803dbee","0x5c11d795","0xb6f9de95",
    "0x472b43f3","0x4a25d94a","0xfb3bdb41"
}

# ERC20 ABI bits / topics
ERC20_ABI = [
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": False, "name": "value", "type": "uint256"}],
     "name": "Transfer", "type": "event"},
    {"inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
]
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
V2_SWAP_TOPIC  = Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
V3_SWAP_TOPIC  = Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

# ===================== Utils =====================
def _fmt_amt(amount: int, decimals: int) -> str:
    d = Decimal(amount) / (Decimal(10) ** decimals)
    return f"{d:.6f}".rstrip("0").rstrip(".")

def _hex_to_addr(topic_hex: Any) -> str:
    if hasattr(topic_hex, "hex"): h = topic_hex.hex()
    else: h = str(topic_hex)
    return "0x" + h[-40:]

def _to_decimal_wei(wei: int) -> Decimal:
    return Decimal(wei) / (Decimal(10) ** 18)

def _ts_from_block(block_ts: int) -> str:
    return datetime.fromtimestamp(block_ts, tz=timezone.utc).isoformat()

def _ensure_ledger_dir():
    d = os.path.dirname(LEDGER_PATH)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

def _append_ledger_row(ts_iso: str, symbol: str, side: str, qty: str, price: str, fee: str, txh: str):
    _ensure_ledger_dir()
    header_needed = not os.path.exists(LEDGER_PATH)
    line = f'{ts_iso},{symbol},{side},{qty},{price},{fee},{txh},cronos\n'
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        if header_needed:
            f.write("ts,symbol,side,qty,price,fee,tx,chain\n")
        f.write(line)

async def _send(send_fn, text: str, notify: bool = True):
    if not notify:
        return
    try:
        if asyncio.iscoroutinefunction(send_fn): await send_fn(text)
        else: send_fn(text)
    except Exception:
        logging.exception("send_fn failed")

async def _erc20_meta(w3: Web3, token: str, cache: Dict[str, Tuple[str, int]]) -> Tuple[str, int]:
    token = token.lower()
    if token in cache: return cache[token]
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        sym = c.functions.symbol().call()
        dec = c.functions.decimals().call()
        cache[token] = (sym, int(dec))
    except Exception:
        cache[token] = (token[:6].upper(), 18)
    return cache[token]

def _looks_like_swap(tx_to: Optional[str], tx_input: Optional[str|bytes], logs: List[LogReceipt]) -> bool:
    if tx_to and tx_to.lower() in KNOWN_ROUTERS: return True
    for lg in logs:
        try:
            t0 = lg["topics"][0].hex() if hasattr(lg["topics"][0], "hex") else str(lg["topics"][0])
            if t0 in (V2_SWAP_TOPIC, V3_SWAP_TOPIC): return True
        except Exception:
            continue
    if isinstance(tx_input, (bytes, bytearray)) and len(tx_input) >= 4:
        if "0x" + tx_input[:4].hex() in SWAP_SELECTORS: return True
    if isinstance(tx_input, str) and tx_input.startswith("0x") and len(tx_input) >= 10:
        if tx_input[:10] in SWAP_SELECTORS: return True
    return False

def _wallet_topic(wallet: str) -> str:
    return "0x" + wallet.lower().replace("0x","").rjust(64, "0")

# ===================== Provider factory with backoff & fallback =====================
async def _make_web3(logger) -> Web3:
    urls: List[Tuple[str,str]] = [("wss", u) for u in WSS_URLS] + [("https", u) for u in HTTPS_URLS]
    random.shuffle(urls)
    for kind, url in urls:
        try:
            if kind == "wss":
                w3 = Web3(Web3.WebsocketProvider(url, websocket_timeout=45))
            else:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 45}))
            if w3.is_connected():
                logger.info("Connected to provider: %s", url)
                return w3
        except Exception as e:
            logger.warning("Provider failed %s: %s", url, e)
    raise RuntimeError("No RPC provider available")

async def _with_backoff(func, *args, logger=None, what="rpc"):
    delay = 1.0
    while True:
        try:
            return func(*args)
        except Exception as e:
            msg = str(e)
            # smarter wait for 429/rate limits
            if "429" in msg or "rate" in msg.lower() or "Too Many Requests" in msg:
                wait = min(BACKOFF_MAX_SEC, delay + random.random()*delay)
            else:
                wait = min(BACKOFF_MAX_SEC, delay)
            if logger:
                logger.warning("%s error (%s). Backing off %.1fs", what, e.__class__.__name__, wait)
            await asyncio.sleep(wait)
            delay = min(BACKOFF_MAX_SEC, delay * 1.8 + 0.5)

# ===================== Core scanners =====================
def _collect_logs_for_wallet(w3: Web3, wallet_lc: str, start_block: int, end_block: int) -> List[LogReceipt]:
    wl_topic = _wallet_topic(wallet_lc)
    logs: List[LogReceipt] = []

    # wallet as sender
    q1 = {"fromBlock": start_block, "toBlock": end_block, "topics": [TRANSFER_TOPIC, wl_topic, None]}
    try:
        logs.extend(w3.eth.get_logs(q1))
    except Exception:
        # minimal retry (upper layer has backoff)
        logs.extend(w3.eth.get_logs(q1))

    # wallet as recipient
    q2 = {"fromBlock": start_block, "toBlock": end_block, "topics": [TRANSFER_TOPIC, None, wl_topic]}
    try:
        logs.extend(w3.eth.get_logs(q2))
    except Exception:
        logs.extend(w3.eth.get_logs(q2))

    return logs

async def _scan_logs_range(w3: Web3, wallet_lc: str, start_block: int, end_block: int, logger, meta_cache, notify, send_fn):
    """
    1) getLogs για Transfer με wallet ως from/to στο [start,end]
    2) Unique tx hashes από logs
    3) Παίρνουμε receipts μόνο για αυτά τα tx
    4) Alerts + ledger
    """
    logs = await _with_backoff(_collect_logs_for_wallet, w3, wallet_lc, start_block, end_block, logger=logger, what="getLogs")
    txhashes: set[str] = set()
    for lg in logs:
        try:
            txhashes.add(lg["transactionHash"].hex())
        except Exception:
            continue

    for txh in txhashes:
        try:
            tx = await _with_backoff(w3.eth.get_transaction, txh, logger=logger, what="getTransaction")
            rc = await _with_backoff(w3.eth.get_transaction_receipt, txh, logger=logger, what="getReceipt")
        except Exception:
            await asyncio.sleep(0.2)
            continue

        # Block ts
        try:
            blk = await _with_backoff(w3.eth.get_block, rc["blockNumber"], logger=logger, what="getBlock")
            ts_iso = _ts_from_block(blk.timestamp)
        except Exception:
            ts_iso = datetime.now(timezone.utc).isoformat()

        # Native CRO
        try:
            if int(tx["value"]) > 0 and (tx["from"].lower() == wallet_lc or (tx["to"] and tx["to"].lower() == wallet_lc)):
                val = _to_decimal_wei(int(tx["value"]))
                native_dir = "OUT" if tx["from"].lower() == wallet_lc else "IN"
                router_note = KNOWN_ROUTERS.get((tx["to"] or "").lower(), "") if tx["to"] else ""
                await _send(send_fn, f"💸 {native_dir} {val:.6f} CRO{(' via '+router_note) if router_note else ''}\nTx: {txh[:10]}…{txh[-8:]}", notify)
                _append_ledger_row(ts_iso, "CRO", "SELL" if native_dir=="OUT" else "BUY", f"{val}", "0", "0", txh)
        except Exception:
            pass

        # ERC-20 legs
        lines: List[str] = []
        for lg in rc["logs"]:
            try:
                topic0 = lg["topics"][0].hex() if hasattr(lg["topics"][0], "hex") else str(lg["topics"][0])
                if topic0 != TRANSFER_TOPIC:
                    continue
                from_addr = _hex_to_addr(lg["topics"][1]).lower()
                to_addr   = _hex_to_addr(lg["topics"][2]).lower()
                if wallet_lc not in (from_addr, to_addr):
                    continue
                token = lg["address"]
                sym, dec = await _erc20_meta(w3, token, meta_cache)
                data = lg["data"]
                amount = int(str(data), 16) if isinstance(data, str) else int.from_bytes(data, "big")
                side = "OUT" if from_addr == wallet_lc else "IN"
                qty_str = _fmt_amt(amount, dec)
                lines.append(f"{side} {qty_str} {sym}")
                _append_ledger_row(ts_iso, sym, "SELL" if side=="OUT" else "BUY", qty_str, "0", "0", txh)
            except Exception:
                continue

        if lines:
            is_swap = _looks_like_swap(tx.get("to"), tx.get("input"), rc["logs"])
            header = "🔄 Swap" if is_swap else "🔔 Transfer"
            router_note = ""
            if tx.get("to") and tx["to"].lower() in KNOWN_ROUTERS:
                router_note = f" via {KNOWN_ROUTERS[tx['to'].lower()]}"
            msg = f"{header}{router_note}\n" + "\n".join(f"• {ln}" for ln in lines) + f"\nTx: {txh[:10]}…{txh[-8:]}"
            await _send(send_fn, msg, notify)

# ===================== Main monitor =====================
async def monitor_wallet(send_fn, logger=logging.getLogger("realtime")):
    if not WALLET:
        logger.warning("No WALLET_ADDRESS set; realtime monitor disabled.")
        return

    # provider with fallback (WSS → HTTPS → next)
    w3 = await _with_backoff(lambda: asyncio.get_event_loop().run_until_complete(_make_web3(logger)), logger=logger, what="provider.connect")

    wallet = Web3.to_checksum_address(WALLET)
    wl = wallet.lower()
    meta_cache: Dict[str, Tuple[str, int]] = {}

    # -------- Startup Backfill (batched getLogs) --------
    try:
        latest = w3.eth.get_block("latest").number
        start  = max(0, latest - BACKFILL_BLOCKS)
        logger.info("Backfill %s..%s (chunk=%s, notify=%s)", start, latest, BACKFILL_CHUNK, BACKFILL_NOTIFY)
        for rng_start in range(start, latest + 1, BACKFILL_CHUNK):
            rng_end = min(latest, rng_start + BACKFILL_CHUNK - 1)
            try:
                await _scan_logs_range(w3, wl, rng_start, rng_end, logger, meta_cache, BACKFILL_NOTIFY, send_fn)
            except Exception as e:
                logger.warning("Backfill chunk %s-%s failed: %s", rng_start, rng_end, e)
                await asyncio.sleep(min(BACKOFF_MAX_SEC, 3.0))
        last_handled = latest
    except Exception as e:
        logger.exception("Backfill failed: %s", e)
        last_handled = w3.eth.get_block("latest").number

    # -------- Live loop (per block; tiny getLogs on 1 block) --------
    while True:
        try:
            latest = w3.eth.get_block("latest").number
            target = latest - CONFIRMATIONS
            if target < last_handled + 1:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for b in range(last_handled + 1, target + 1):
                try:
                    await _scan_logs_range(w3, wl, b, b, logger, meta_cache, True, send_fn)
                except Exception as e:
                    logger.warning("Block %s scan failed: %s", b, e)
                    await asyncio.sleep(min(BACKOFF_MAX_SEC, 3.0))
                last_handled = b

            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.warning("Live loop error: %s — reconnecting provider…", e)
            # reconnect provider with backoff
            try:
                w3 = await _with_backoff(lambda: asyncio.get_event_loop().run_until_complete(_make_web3(logger)), logger=logger, what="provider.reconnect")
            except Exception as e2:
                logger.error("Provider reconnect failed: %s", e2)
                await asyncio.sleep(min(BACKOFF_MAX_SEC, 5.0))
