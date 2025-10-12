# realtime/monitor.py
from __future__ import annotations
import os
import asyncio
import logging
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal

from web3 import Web3
from web3.types import LogReceipt

# --- ENV / Defaults ---
WALLET = (os.getenv("WALLET_ADDRESS") or "").strip()
CRONOS_WSS = os.getenv("CRONOS_WSS_URL", "wss://cronos-evm-rpc.publicnode.com")
# 0 = push as soon as it lands in latest block; >=1 waits confirmations before pushing
CONFIRMATIONS = int(os.getenv("RT_CONFIRMS", "0"))
POLL_INTERVAL = float(os.getenv("RT_POLL_SEC", "1.0"))

# --- Known routers on Cronos (expand easily) ---
KNOWN_ROUTERS: Dict[str, str] = {
    # VVS Finance
    "0x145863eb42cf62847a6ca784e6416c1682b1b2ae": "VVS Router",
    "0xe0137ee596c35bf7adedad1e2fd25da595d1e05b": "VVS Router (alt)",
    # MM.Finance
    "0x22d710931f01c1681ca1570ff016ed42eb7b7c2a": "MMF Router",
    # Odos (aggregator)
    "0x76c930c6a2c2d7ee1e2fcfef05b0bb2e6902a84a": "Odos Router",
    # Crodex
    "0x62e60a3f73d3f90a5a2f0a6a3e0f6c2a1e86c9b9": "Crodex Router",
    # Veno / Ferro (stable swap routers commonly used)
    "0xdef1abe32c034e558cdd535791643c58a13acc10": "Router (common agg)",  # generic agg fallback
}
# lowercase keys
KNOWN_ROUTERS = {k.lower(): v for k, v in KNOWN_ROUTERS.items()}

# --- Common router method selectors (first 4 bytes) to tag swaps even if logs are sparse ---
# These are typical for UniV2-like and router-style swaps.
SWAP_SELECTORS = {
    "0x38ed1739",  # swapExactTokensForTokens(uint,uint,address[],address,uint)
    "0x18cbafe5",  # swapExactTokensForETH(uint,uint,address[],address,uint)
    "0x7ff36ab5",  # swapExactETHForTokens(uint,uint,address[],address,uint)
    "0x8803dbee",  # swapExactTokensForTokensSupportingFeeOnTransferTokens(...)
    "0x5c11d795",  # swapExactTokensForETHSupportingFeeOnTransferTokens(...)
    "0xb6f9de95",  # swapExactETHForTokensSupportingFeeOnTransferTokens(...)
    "0x472b43f3",  # swapTokensForExactTokens(uint,uint,address[],address,uint)
    "0x4a25d94a",  # swapTokensForExactETH(uint,uint,address[],address,uint)
    "0xfb3bdb41",  # swapETHForExactTokens(uint,uint,address[],address,uint)
}

# --- ABIs / Topics ---
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

# Uniswap V2 Swap event:
V2_SWAP_TOPIC = Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
# Uniswap V3 Swap event:
V3_SWAP_TOPIC = Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()

def _fmt_amt(amount: int, decimals: int) -> str:
    d = Decimal(amount) / (Decimal(10) ** decimals)
    return f"{d:.6f}".rstrip("0").rstrip(".")

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

def _hex_to_addr(topic_hex: Any) -> str:
    # topic_hex may be HexBytes or hex-string; normalize to '0x....'
    if hasattr(topic_hex, "hex"):
        h = topic_hex.hex()
    else:
        h = str(topic_hex)
    return "0x" + h[-40:]

def _parse_transfers(logs: List[LogReceipt], wallet: str) -> List[Dict[str, Any]]:
    outs = []
    w = wallet.lower()
    for lg in logs:
        try:
            if (lg["topics"][0].hex() if hasattr(lg["topics"][0], "hex") else str(lg["topics"][0])) != TRANSFER_TOPIC:
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

def _looks_like_swap(tx_to: Optional[str], tx_input: Optional[str], logs: List[LogReceipt]) -> bool:
    if tx_to and tx_to.lower() in KNOWN_ROUTERS:
        return True
    # any V2 or V3 Swap in logs
    for lg in logs:
        try:
            topic0 = lg["topics"][0].hex() if hasattr(lg["topics"][0], "hex") else str(lg["topics"][0])
            if topic0 in (V2_SWAP_TOPIC, V3_SWAP_TOPIC):
                return True
        except Exception:
            continue
    # method selector on router calls
    if isinstance(tx_input, (bytes, bytearray)) and len(tx_input) >= 4:
        selector = "0x" + tx_input[:4].hex()
        if selector in SWAP_SELECTORS:
            return True
    if isinstance(tx_input, str) and tx_input.startswith("0x") and len(tx_input) >= 10:
        selector = tx_input[:10]
        if selector in SWAP_SELECTORS:
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
                # '0x...' string
                amount = int(str(data), 16)
        except Exception:
            amount = 0
        direction = "IN" if t["to"].lower() == WALLET.lower() else "OUT"
        lines.append(f"{direction} {_fmt_amt(amount, dec)} {sym}")
    return lines

async def _send(send_fn, text: str):
    try:
        if asyncio.iscoroutinefunction(send_fn):
            await send_fn(text)
        else:
            send_fn(text)
    except Exception:
        logging.exception("send_fn failed")

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

    while True:
        try:
            latest = w3.eth.get_block("latest")
            latest_num = latest.number

            if last_handled is None:
                # start from (latest - 1) so we don't miss just-mined block
                last_handled = max(0, latest_num - 1)

            # Process up to latest - CONFIRMATIONS
            target = latest_num - CONFIRMATIONS
            if target < last_handled + 1:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            for b in range(last_handled + 1, target + 1):
                blk = w3.eth.get_block(b, full_transactions=True)

                # 1) Native CRO movements involving wallet
                for tx in blk.transactions:
                    try:
                        frm = (tx["from"] or "").lower()
                        to  = (tx["to"] or "").lower() if tx["to"] else ""
                        if wallet.lower() not in (frm, to):
                            continue
                        if int(tx["value"]) > 0:
                            val = Decimal(tx["value"]) / (Decimal(10) ** 18)
                            native_dir = "OUT" if frm == wallet.lower() else "IN"
                            router_note = KNOWN_ROUTERS.get(to, "") if to else ""
                            note = f" via {router_note}" if router_note else ""
                            await _send(send_fn, f"💸 {native_dir} {val:.6f} CRO{note}\nTx: {tx['hash'].hex()[:10]}…{tx['hash'].hex()[-8:]}")
                    except Exception:
                        continue

                # 2) ERC-20 Transfers (filter at node by topic, then post-filter by wallet)
                logs = w3.eth.get_logs({
                    "fromBlock": b,
                    "toBlock": b,
                    "topics": [TRANSFER_TOPIC, None, None],
                })
                logs_for_wallet = []
                wl = wallet.lower()
                for lg in logs:
                    try:
                        from_addr = _hex_to_addr(lg["topics"][1]).lower()
                        to_addr   = _hex_to_addr(lg["topics"][2]).lower()
                        if wl in (from_addr, to_addr):
                            logs_for_wallet.append(lg)
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
                    except Exception:
                        continue

                last_handled = b

            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.exception("monitor loop error: %s", e)
            await asyncio.sleep(3.0)
