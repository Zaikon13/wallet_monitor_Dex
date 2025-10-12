# realtime/monitor.py
from __future__ import annotations
import os
import asyncio
import logging
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal
from datetime import datetime, timezone

from web3 import Web3
from web3.types import LogReceipt

# ------------ ENV / Defaults ------------
WALLET = (os.getenv("WALLET_ADDRESS") or "").strip()
CRONOS_WSS = os.getenv("CRONOS_WSS_URL", "wss://cronos-evm-rpc.publicnode.com")

# Live loop
CONFIRMATIONS = int(os.getenv("RT_CONFIRMS", "0"))           # 0 = instant
POLL_INTERVAL = float(os.getenv("RT_POLL_SEC", "0.8"))

# Ledger
LEDGER_PATH = os.getenv("LEDGER_CSV", "data/ledger.csv")

# Startup backfill
BACKFILL_BLOCKS = int(os.getenv("RT_BACKFILL_BLOCKS", "5000"))   # ~hours of history, adjust
BACKFILL_NOTIFY = (os.getenv("RT_BACKFILL_NOTIFY", "0").strip() in ("1","true","yes"))

# Known routers (extendable)
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

# ABIs / topics
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
V2_SWAP_TOPIC = Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
V3_SWAP_TOPIC = Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

# ------------ Utils ------------
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
    """
    CSV schema:
    ts,symbol,side,qty,price,fee,tx,chain
    """
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

# ------------ Core scan (shared by backfill & live) ------------
async def _scan_block(w3: Web3, wallet_lc: str, block_num: int, send_fn, meta_cache, notify=True):
    blk = w3.eth.get_block(block_num, full_transactions=True)
    ts_iso = _ts_from_block(blk.timestamp)

    # (A) Native CRO IN/OUT
    for tx in blk.transactions:
        try:
            frm = (tx["from"] or "").lower()
            to  = (tx["to"] or "").lower() if tx["to"] else ""
            if wallet_lc not in (frm, to): 
                continue
            if int(tx["value"]) > 0:
                val = _to_decimal_wei(int(tx["value"]))
                native_dir = "OUT" if frm == wallet_lc else "IN"
                router_note = KNOWN_ROUTERS.get(to, "") if to else ""
                txh = tx['hash'].hex()
                await _send(send_fn, f"💸 {native_dir} {val:.6f} CRO{(' via '+router_note) if router_note else ''}\nTx: {txh[:10]}…{txh[-8:]}", notify)
                _append_ledger_row(ts_iso, "CRO", "SELL" if native_dir=="OUT" else "BUY", f"{val}", "0", "0", txh)
        except Exception:
            continue

    # (B) ERC-20 Transfers touching wallet — scan EVERY receipt (never miss)
    by_tx: Dict[str, List[LogReceipt]] = {}
    for tx in blk.transactions:
        try:
            rc = w3.eth.get_transaction_receipt(tx["hash"])
        except Exception:
            continue
        for lg in rc["logs"]:
            try:
                topic0 = lg["topics"][0].hex() if hasattr(lg["topics"][0], "hex") else str(lg["topics"][0])
                if topic0 != TRANSFER_TOPIC: 
                    continue
                from_addr = _hex_to_addr(lg["topics"][1]).lower()
                to_addr   = _hex_to_addr(lg["topics"][2]).lower()
                if wallet_lc not in (from_addr, to_addr):
                    continue
                by_tx.setdefault(tx["hash"].hex(), []).append(lg)
            except Exception:
                continue

    for txh, lgs in by_tx.items():
        try:
            tx = w3.eth.get_transaction(txh)
            rc = w3.eth.get_transaction_receipt(txh)
            lines = []
            for lg in lgs:
                token = lg["address"]
                sym, dec = await _erc20_meta(w3, token, meta_cache)
                data = lg["data"]
                try:
                    amount = int(str(data), 16) if isinstance(data, str) else int.from_bytes(data, "big")
                except Exception:
                    amount = 0
                from_addr = _hex_to_addr(lg["topics"][1]).lower()
                side = "OUT" if from_addr == wallet_lc else "IN"
                qty_str = _fmt_amt(amount, dec)
                lines.append(f"{side} {qty_str} {sym}")
                _append_ledger_row(ts_iso, sym, "SELL" if side=="OUT" else "BUY", qty_str, "0", "0", txh)

            is_swap = _looks_like_swap(tx["to"], tx.get("input"), rc["logs"])
            header = "🔄 Swap" if is_swap else "🔔 Transfer"
            router_note = ""
            if tx["to"] and tx["to"].lower() in KNOWN_ROUTERS:
                router_note = f" via {KNOWN_ROUTERS[tx['to'].lower()]}"
            msg = f"{header}{router_note}\n" + "\n".join(f"• {ln}" for ln in lines) + f"\nTx: {txh[:10]}…{txh[-8:]}"
            await _send(send_fn, msg, notify)
        except Exception:
            continue

# ------------ Main ------------
async def monitor_wallet(send_fn, logger=logging.getLogger("realtime")):
    if not WALLET:
        logger.warning("No WALLET_ADDRESS set; realtime monitor disabled.")
        return

    w3 = Web3(Web3.WebsocketProvider(CRONOS_WSS, websocket_timeout=45))
    if not w3.is_connected():
        logger.error("Web3 not connected to %s", CRONOS_WSS)
        return

    logger.info("Realtime monitor connected to %s", CRONOS_WSS)
    wallet = Web3.to_checksum_address(WALLET)
    wl = wallet.lower()
    meta_cache: Dict[str, Tuple[str, int]] = {}

    # --- Startup backfill ---
    try:
        latest = w3.eth.get_block("latest").number
        start = max(0, latest - BACKFILL_BLOCKS)
        logger.info("Backfilling blocks %s..%s (notify=%s)", start, latest, BACKFILL_NOTIFY)
        for b in range(start, latest + 1):
            await _scan_block(w3, wl, b, send_fn, meta_cache, notify=BACKFILL_NOTIFY)
        last_handled = latest
    except Exception as e:
        logger.exception("Backfill failed: %s", e)
        last_handled = w3.eth.get_block("latest").number

    # --- Live loop ---
    while True:
        try:
            latest = w3.eth.get_block("latest").number
            target = latest - CONFIRMATIONS
            if target < last_handled + 1:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for b in range(last_handled + 1, target + 1):
                await _scan_block(w3, wl, b, send_fn, meta_cache, notify=True)
                last_handled = b

            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.exception("monitor loop error: %s", e)
            await asyncio.sleep(3.0)
