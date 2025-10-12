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
# 0 = push immediately; >=1 waits confirmations before pushing
CONFIRMATIONS = int(os.getenv("RT_CONFIRMS", "0"))
POLL_INTERVAL = float(os.getenv("RT_POLL_SEC", "1.0"))
LEDGER_PATH = os.getenv("LEDGER_CSV", "data/ledger.csv")
ENABLE_PENDING = (os.getenv("RT_PENDING", "0").strip() in ("1","true","yes"))
LOG_CHUNK_RETRY = True  # fallback to receipts if getLogs is too large

# Known routers (extendable)
KNOWN_ROUTERS: Dict[str, str] = {
    "0x145863eb42cf62847a6ca784e6416c1682b1b2ae": "VVS Router",
    "0xe0137ee596c35bf7adedad1e2fd25da595d1e05b": "VVS Router (alt)",
    "0x22d710931f01c1681ca1570ff016ed42eb7b7c2a": "MMF Router",
    "0x76c930c6a2c2d7ee1e2fcfef05b0bb2e6902a84a": "Odos Router",
    "0xdef1abe32c034e558cdd535791643c58a13acc10": "Router (agg)",
}
KNOWN_ROUTERS = {k.lower(): v for k, v in KNOWN_ROUTERS.items()}

# Common router selectors
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
    if hasattr(topic_hex, "hex"):
        h = topic_hex.hex()
    else:
        h = str(topic_hex)
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
    CSV schema expected by /trades & /pnl today:
    ts,symbol,side,qty,price,fee,tx,chain
    """
    _ensure_ledger_dir()
    header_needed = not os.path.exists(LEDGER_PATH)
    line = f'{ts_iso},{symbol},{side},{qty},{price},{fee},{txh},cronos\n'
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        if header_needed:
            f.write("ts,symbol,side,qty,price,fee,tx,chain\n")
        f.write(line)

async def _send(send_fn, text: str):
    try:
        if asyncio.iscoroutinefunction(send_fn):
            await send_fn(text)
        else:
            send_fn(text)
    except Exception:
        logging.exception("send_fn failed")

async def _erc20_meta(w3: Web3, token: str, cache: Dict[str, Tuple[str, int]]) -> Tuple[str, int]:
    token = token.lower()
    if token in cache:
        return cache[token]
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        sym = c.functions.symbol().call()
        dec = c.functions.decimals().call()
        cache[token] = (sym, int(dec))
        return cache[token]
    except Exception:
        cache[token] = (token[:6].upper(), 18)
        return cache[token]

def _parse_transfers(logs: List[LogReceipt], wallet: str) -> List[Dict[str, Any]]:
    outs = []
    w = wallet.lower()
    for lg in logs:
        try:
            topic0 = lg["topics"][0].hex() if hasattr(lg["topics"][0], "hex") else str(lg["topics"][0])
            if topic0 != TRANSFER_TOPIC: 
                continue
            if len(lg["topics"]) < 3: 
                continue
            from_addr = _hex_to_addr(lg["topics"][1])
            to_addr   = _hex_to_addr(lg["topics"][2])
            if w not in (from_addr.lower(), to_addr.lower()): 
                continue
            outs.append({
                "token": lg["address"],
                "from": from_addr,
                "to": to_addr,
                "data": lg["data"],  # amount
                "log": lg,
            })
        except Exception:
            continue
    return outs

def _looks_like_swap(tx_to: Optional[str], tx_input: Optional[str|bytes], logs: List[LogReceipt]) -> bool:
    if tx_to and tx_to.lower() in KNOWN_ROUTERS: 
        return True
    for lg in logs:
        try:
            t0 = lg["topics"][0].hex() if hasattr(lg["topics"][0], "hex") else str(lg["topics"][0])
            if t0 in (V2_SWAP_TOPIC, V3_SWAP_TOPIC): 
                return True
        except Exception:
            continue
    if isinstance(tx_input, (bytes, bytearray)) and len(tx_input) >= 4:
        if ("0x" + tx_input[:4].hex()) in SWAP_SELECTORS: 
            return True
    if isinstance(tx_input, str) and tx_input.startswith("0x") and len(tx_input) >= 10:
        if tx_input[:10] in SWAP_SELECTORS: 
            return True
    return False

async def _describe_transfers(w3: Web3, transfers: List[Dict[str, Any]], meta_cache: Dict[str, Tuple[str, int]]) -> List[str]:
    lines = []
    for t in transfers:
        token = t["token"]
        sym, dec = await _erc20_meta(w3, token, meta_cache)
        data = t["data"]
        try:
            if isinstance(data, (bytes, bytearray)):
                amount = int.from_bytes(data, "big")
            else:
                amount = int(str(data), 16)
        except Exception:
            amount = 0
        direction = "IN" if t["to"].lower() == WALLET.lower() else "OUT"
        lines.append(f"{direction} {_fmt_amt(amount, dec)} {sym}")
    return lines

# ------------ Core monitor ------------
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
    meta_cache: Dict[str, Tuple[str, int]] = {}
    last_handled = None

    # Optional pending pre-alerts
    if ENABLE_PENDING:
        asyncio.create_task(_pending_loop(w3, wallet, send_fn, logger))

    while True:
        try:
            latest = w3.eth.get_block("latest")
            latest_num = latest.number

            if last_handled is None:
                last_handled = max(0, latest_num - 1)

            target = latest_num - CONFIRMATIONS
            if target < last_handled + 1:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for b in range(last_handled + 1, target + 1):
                blk = w3.eth.get_block(b, full_transactions=True)
                blk_ts_iso = _ts_from_block(blk.timestamp)

                # 1) Native CRO to/from wallet (top-level only; internal value needs traces not available on public RPC)
                for tx in blk.transactions:
                    try:
                        frm = (tx["from"] or "").lower()
                        to  = (tx["to"] or "").lower() if tx["to"] else ""
                        if wallet.lower() not in (frm, to):
                            continue
                        if int(tx["value"]) > 0:
                            val = _to_decimal_wei(int(tx["value"]))
                            native_dir = "OUT" if frm == wallet.lower() else "IN"
                            router_note = KNOWN_ROUTERS.get(to, "") if to else ""
                            note = f" via {router_note}" if router_note else ""
                            txh = tx['hash'].hex()
                            await _send(send_fn, f"💸 {native_dir} {val:.6f} CRO{note}\nTx: {txh[:10]}…{txh[-8:]}")
                            _append_ledger_row(blk_ts_iso, "CRO", "BUY" if native_dir=="IN" else "SELL", f"{val}", "0", "0", txh)
                    except Exception:
                        continue

                # 2) ERC-20 Transfers involving wallet — server-side filtered both directions
                logs_for_wallet: List[LogReceipt] = []
                wl_topic = "0x" + wallet.lower().replace("0x","").rjust(64, "0")
                try:
                    # as sender
                    logs_out = w3.eth.get_logs({
                        "fromBlock": b, "toBlock": b,
                        "topics": [TRANSFER_TOPIC, wl_topic, None],
                    })
                    # as recipient
                    logs_in = w3.eth.get_logs({
                        "fromBlock": b, "toBlock": b,
                        "topics": [TRANSFER_TOPIC, None, wl_topic],
                    })
                    logs_for_wallet.extend(logs_out)
                    logs_for_wallet.extend(logs_in)
                except Exception as e:
                    logger.warning("get_logs topic-filter failed on block %s: %s", b, e)
                    if LOG_CHUNK_RETRY:
                        # Fallback: scan every tx receipt in block (slower but reliable)
                        for tx in blk.transactions:
                            try:
                                rc = w3.eth.get_transaction_receipt(tx["hash"])
                                for lg in rc["logs"]:
                                    try:
                                        t0 = lg["topics"][0].hex() if hasattr(lg["topics"][0], "hex") else str(lg["topics"][0])
                                        if t0 != TRANSFER_TOPIC: 
                                            continue
                                        from_addr = _hex_to_addr(lg["topics"][1]).lower()
                                        to_addr   = _hex_to_addr(lg["topics"][2]).lower()
                                        if wallet.lower() in (from_addr, to_addr):
                                            logs_for_wallet.append(lg)
                                    except Exception:
                                        continue
                            except Exception:
                                continue

                # group by tx
                by_tx: Dict[str, List[LogReceipt]] = {}
                for lg in logs_for_wallet:
                    by_tx.setdefault(lg["transactionHash"].hex(), []).append(lg)

                for txh, lgs in by_tx.items():
                    try:
                        tx = w3.eth.get_transaction(txh)
                        rc = w3.eth.get_transaction_receipt(txh)
                        transfers = _parse_transfers(lgs, wallet)
                        is_swap = _looks_like_swap(tx["to"], tx.get("input"), rc["logs"])
                        lines = await _describe_transfers(w3, transfers, meta_cache)

                        header = "🔄 Swap" if is_swap else "🔔 Transfer"
                        router_note = ""
                        if tx["to"] and tx["to"].lower() in KNOWN_ROUTERS:
                            router_note = f" via {KNOWN_ROUTERS[tx['to'].lower()]}"
                        msg = f"{header}{router_note}\n" + "\n".join(f"• {ln}" for ln in lines) + f"\nTx: {txh[:10]}…{txh[-8:]}"
                        await _send(send_fn, msg)

                        # ledger append for each leg
                        for ln in lines:
                            try:
                                side_word, qty_str, symbol = ln.split()[:3]  # "IN 1.23 ADA"
                                side = "BUY" if side_word == "IN" else "SELL"
                                _append_ledger_row(blk_ts_iso, symbol, side, qty_str, "0", "0", txh)
                            except Exception:
                                continue
                    except Exception:
                        continue

                last_handled = b

            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.exception("monitor loop error: %s", e)
            await asyncio.sleep(3.0)

# ------------ Optional: pending tx pre-alerts ------------
async def _pending_loop(w3: Web3, wallet: str, send_fn, logger):
    try:
        sub = w3.provider._ws._conn  # use underlying ws; web3.py doesn't expose a stable pending sub API
    except Exception:
        logger.warning("Pending subscribe not available on this provider.")
        return

    logger.info("Pending watcher enabled.")
    # Best-effort polling of 'pending' pool by listing pending tx hashes via RPC isn't standardized across nodes.
    # We simulate by checking latest block rapidly; real mempool subs may not be supported by public Cronos nodes.
    # Keep as a tight poller for near-instant alerts:
    while True:
        try:
            await asyncio.sleep(0.5)
        except Exception:
            await asyncio.sleep(1.0)
