# realtime/monitor.py
from __future__ import annotations
import os
import asyncio
import json
import logging
from typing import Optional, Dict, Any, List, Tuple
from decimal import Decimal

from web3 import Web3
from web3.types import LogReceipt

# --- ENV / Defaults ---
WALLET = (os.getenv("WALLET_ADDRESS") or "").strip()
CRONOS_WSS = os.getenv("CRONOS_WSS_URL", "wss://cronos-evm-rpc.publicnode.com")  # public WS
CONFIRMATIONS = int(os.getenv("RT_CONFIRMS", "2"))  # πόσα blocks περιμένουμε πριν ενημερώσουμε
POLL_INTERVAL = float(os.getenv("RT_POLL_SEC", "1.2"))  # sec ανά νέο block polling

# Γνωστοί routers στο Cronos (μπορείς να προσθέσεις/αλλάξεις χωρίς restart αν τα περάσεις σε ENV)
KNOWN_ROUTERS = {
    # VVS Finance Router:
    # Σύμφωνα με Cronos explorer labels (router address εμφανίζεται ως "VVS Finance: Router").
    "0x145863Eb42Cf62847A6Ca784e6416C1682b1b2Ae".lower(): "VVS Router",
    "0xe0137ee596c35bf7adedad1e2fd25da595d1e05b".lower(): "VVS Router (alt)",  # επιπλέον label που έχει εμφανιστεί
    # MM.Finance (σύνηθες router main):
    "0x22d710931f01c1681ca1570ff016ed42eb7b7c2a".lower(): "MMF Router",
}

# ERC20 ABI μίνιμαλ για symbol/decimals και Transfer event
ERC20_ABI = [
    {"anonymous": False, "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": False, "name": "value", "type": "uint256"}],
     "name": "Transfer", "type": "event"},
    {"inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "stateMutability": "view", "type": "function"},
]

# Pair Swap event (για pools τύπου UniswapV2)
SWAP_TOPIC = Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()

def _fmt_amt(amount: int, decimals: int) -> str:
    d = Decimal(amount) / (Decimal(10) ** decimals)
    # 0.######## έως 4 δεκαδικά
    return f"{d:.6f}".rstrip("0").rstrip(".")

async def _erc20_meta(w3: Web3, token: str) -> Tuple[str, int]:
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        sym = c.functions.symbol().call()
        dec = c.functions.decimals().call()
        return sym, int(dec)
    except Exception:
        # fallback: κόβουμε το address
        return token[:6].upper(), 18

def _parse_transfers(logs: List[LogReceipt], wallet: str) -> List[Dict[str, Any]]:
    """Επιστρέφει transfers (in/out) που εμπλέκουν το wallet."""
    outs = []
    wallet_low = wallet.lower()
    for lg in logs:
        if lg["topics"][0].hex() != TRANSFER_TOPIC:
            continue
        # topics[1]=from, topics[2]=to
        if len(lg["topics"]) < 3:
            continue
        from_addr = "0x" + lg["topics"][1].hex()[-40:]
        to_addr = "0x" + lg["topics"][2].hex()[-40:]
        if wallet_low not in (from_addr.lower(), to_addr.lower()):
            continue
        outs.append({
            "token": lg["address"],
            "from": from_addr,
            "to": to_addr,
            "data": lg["data"],  # amount στο data
            "log": lg,
        })
    return outs

def _is_probable_swap(tx_to: Optional[str], transfers_count: int, logs: List[LogReceipt]) -> bool:
    """Εύρεση swap: είτε tx.to είναι γνωστό router, είτε έχουμε Swap event σε ζεύγος."""
    if tx_to and tx_to.lower() in KNOWN_ROUTERS:
        return True
    if any((len(x["topics"]) > 0 and x["topics"][0].hex() == SWAP_TOPIC) for x in logs):
        return True
    # σήκωσε heuristic: >=2 transfers στο ίδιο tx (in+out) → πιθανός swap
    return transfers_count >= 2

async def _describe_transfers(w3: Web3, transfers: List[Dict[str, Any]]) -> List[str]:
    lines = []
    meta_cache: Dict[str, Tuple[str, int]] = {}
    for t in transfers:
        token = t["token"]
        if token not in meta_cache:
            meta_cache[token] = await _erc20_meta(w3, token)
        sym, dec = meta_cache[token]
        amount = int(t["data"], 16) if isinstance(t["data"], str) else int.from_bytes(t["data"], "big")
        direction = "IN" if t["to"].lower() == WALLET.lower() else "OUT"
        lines.append(f"{direction} { _fmt_amt(amount, dec) } {sym}")
    return lines

async def monitor_wallet(send_fn, logger=logging.getLogger("realtime")):
    """
    send_fn: async | sync function(text: str) που στέλνει στο Telegram (θα δώσουμε wrapper από app.py)
    """
    if not WALLET:
        logger.warning("No WALLET_ADDRESS set; realtime monitor disabled.")
        return

    w3 = Web3(Web3.WebsocketProvider(CRONOS_WSS, websocket_timeout=30))
    if not w3.is_connected():
        logger.error("Web3 not connected to %s", CRONOS_WSS)
        return

    logger.info("Realtime monitor connected to %s", CRONOS_WSS)
    wallet = Web3.to_checksum_address(WALLET)

    last_handled_block = None
    pending_msgs: Dict[str, Dict[str, Any]] = {}  # txHash -> data

    while True:
        try:
            # περίμενε νέο block
            block = w3.eth.get_block("latest")
            number = block.number
            if last_handled_block is None:
                last_handled_block = max(0, number - 1)
            # προχώρα block-by-block για σταθερότητα
            for b in range(last_handled_block + 1, number - CONFIRMATIONS + 1):
                blk = w3.eth.get_block(b, full_transactions=True)
                # 1) native CRO κινήσεις: απευθείας txs προς/από wallet
                for tx in blk.transactions:
                    frm = (tx["from"] or "").lower()
                    to = (tx["to"] or "").lower() if tx["to"] else ""
                    if wallet.lower() in (frm, to):
                        val = Decimal(tx["value"]) / (Decimal(10) ** 18)
                        native_dir = "OUT" if frm == wallet.lower() else "IN"
                        maybe_router = KNOWN_ROUTERS.get(to, "") if to else ""
                        note = f" ({maybe_router})" if maybe_router else ""
                        text = f"💸 {native_dir} {val:.6f} CRO — tx {tx['hash'].hex()[:10]}…{tx['hash'].hex()[-8:]}{note}"
                        await _safe_send(send_fn, text)

                # 2) ERC20 Transfers + Swaps
                # φέρε όλα τα logs του block που μας αφορούν
                logs = w3.eth.get_logs({
                    "fromBlock": b,
                    "toBlock": b,
                    "topics": [TRANSFER_TOPIC, None, None],  # φίλτρο στο event
                })
                # φιλτράρισμα μόνο όσων αφορούν το wallet
                logs_for_wallet = [lg for lg in logs if (
                    ("0x" + lg["topics"][1].hex()[-40:]).lower() == wallet.lower() or
                    ("0x" + lg["topics"][2].hex()[-40:]).lower() == wallet.lower()
                )]

                # ομαδοποίηση ανά tx
                by_tx: Dict[str, List[LogReceipt]] = {}
                for lg in logs_for_wallet:
                    h = lg["transactionHash"].hex()
                    by_tx.setdefault(h, []).append(lg)

                for txh, lgs in by_tx.items():
                    # φέρε την tx & receipt για context (to-address, extra logs για Swap)
                    tx = w3.eth.get_transaction(txh)
                    rc = w3.eth.get_transaction_receipt(txh)
                    transfers = _parse_transfers(lgs, wallet)
                    is_swap = _is_probable_swap(tx["to"], len(transfers), rc["logs"])

                    # format γραμμές
                    lines = await _describe_transfers(w3, transfers)
                    header = "🔄 Swap" if is_swap else "🔔 Transfer"
                    router_note = ""
                    if tx["to"] and tx["to"].lower() in KNOWN_ROUTERS:
                        router_note = f" via {KNOWN_ROUTERS[tx['to'].lower()]}"
                    msg = f"{header}{router_note}\n" + "\n".join(f"• {ln}" for ln in lines) + f"\nTx: {txh[:10]}…{txh[-8:]}"
                    await _safe_send(send_fn, msg)

                last_handled_block = b

            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.exception("monitor loop error: %s", e)
            await asyncio.sleep(3.0)

async def _safe_send(send_fn, text: str):
    try:
        if asyncio.iscoroutinefunction(send_fn):
            await send_fn(text)
        else:
            send_fn(text)
    except Exception:
        logging.exception("send_fn failed")
