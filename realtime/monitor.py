# realtime/monitor.py
from __future__ import annotations
import os, asyncio, logging, random
from typing import Dict, List, Tuple, Optional, Any
from decimal import Decimal
from datetime import datetime, timezone
from web3 import Web3
from web3.types import LogReceipt

WALLET = (os.getenv("WALLET_ADDRESS") or "").strip()
# Endpoints (WSS optional, HTTPS υποστηρίζει και CRONOS_RPC_URL)
WSS_URLS = [u.strip() for u in (os.getenv("CRONOS_WSS_URL", "").split(",")) if u.strip()]
_https_env = os.getenv("CRONOS_HTTPS_URL") or os.getenv("CRONOS_RPC_URL") or "https://cronos-evm-rpc.publicnode.com"
HTTPS_URLS = [u.strip() for u in _https_env.split(",") if u.strip()]

CONFIRMATIONS     = int(os.getenv("RT_CONFIRMS", "0"))
POLL_INTERVAL     = float(os.getenv("RT_POLL_SEC", "0.8"))
LEDGER_PATH       = os.getenv("LEDGER_CSV", "data/ledger.csv")
BACKFILL_BLOCKS   = int(os.getenv("RT_BACKFILL_BLOCKS", "3000"))
BACKFILL_NOTIFY   = (os.getenv("RT_BACKFILL_NOTIFY", "0").strip() in ("1","true","yes"))
BACKFILL_CHUNK    = int(os.getenv("RT_BACKFILL_CHUNK", "400"))
BACKOFF_MAX_SEC   = float(os.getenv("RT_BACKOFF_MAX_SEC", "20"))

# Routers (μπορείς να επεκτείνεις)
KNOWN_ROUTERS: Dict[str, str] = {
    "0x145863eb42cf62847a6ca784e6416c1682b1b2ae": "VVS Router",
    "0xe0137ee596c35bf7adedad1e2fd25da595d1e05b": "VVS Router (alt)",
    "0x22d710931f01c1681ca1570ff016ed42eb7b7c2a": "MMF Router",
    "0x76c930c6a2c2d7ee1e2fcfef05b0bb2e6902a84a": "Odos Router",
    "0xdef1abe32c034e558cdd535791643c58a13acc10": "Router (agg)",
}
KNOWN_ROUTERS = {k.lower(): v for k, v in KNOWN_ROUTERS.items()}

# Selectors: common swaps
SWAP_SELECTORS = {"0x38ed1739","0x18cbafe5","0x7ff36ab5","0x8803dbee","0x5c11d795","0xb6f9de95","0x472b43f3","0x4a25d94a","0xfb3bdb41"}

# Topics
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
V2_SWAP_TOPIC  = Web3.keccak(text="Swap(address,uint256,uint256,uint256,uint256,address)").hex()
V3_SWAP_TOPIC  = Web3.keccak(text="Swap(address,address,int256,int256,uint160,uint128,int24)").hex()
DEPOSIT_TOPIC  = Web3.keccak(text="Deposit(address,uint256)").hex()     # WCRO/WETH9 wrap
WITHDRAW_TOPIC = Web3.keccak(text="Withdrawal(address,uint256)").hex()  # WCRO/WETH9 unwrap

def _fmt_amt(amount: int, decimals: int) -> str:
    d = Decimal(amount) / (Decimal(10) ** decimals)
    return f"{d:.6f}".rstrip("0").rstrip(".")

def _to_dec18(wei: int) -> Decimal:
    return Decimal(wei) / (Decimal(10) ** 18)

def _hex_addr(x) -> str:
    h = x.hex() if hasattr(x, "hex") else str(x)
    return "0x" + h[-40:]

def _wallet_topic(addr: str) -> str:
    return "0x" + addr.lower().replace("0x","").rjust(64, "0")

def _ts(block_ts: int) -> str:
    return datetime.fromtimestamp(block_ts, tz=timezone.utc).isoformat()

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

def _append_ledger(ts_iso: str, symbol: str, side: str, qty: str, price: str, fee: str, txh: str):
    _ensure_dir(LEDGER_PATH)
    header = not os.path.exists(LEDGER_PATH)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        if header:
            f.write("ts,symbol,side,qty,price,fee,tx,chain\n")
        f.write(f"{ts_iso},{symbol},{side},{qty},{price},{fee},{txh},cronos\n")

def _make_web3(logger) -> Web3:
    urls: List[Tuple[str,str]] = [("wss", u) for u in WSS_URLS] + [("https", u) for u in HTTPS_URLS]
    random.shuffle(urls)
    for kind, url in urls:
        try:
            w3 = Web3(Web3.WebsocketProvider(url, websocket_timeout=45)) if kind=="wss" else Web3(Web3.HTTPProvider(url, request_kwargs={"timeout":45}))
            if w3.is_connected():
                logger.info("Connected RPC: %s", url)
                return w3
        except Exception as e:
            logger.warning("RPC failed %s: %s", url, e)
    raise RuntimeError("No RPC provider available")

async def _send(send_fn, text: str, notify=True):
    if not notify: return
    try:
        if asyncio.iscoroutinefunction(send_fn): await send_fn(text)
        else: send_fn(text)
    except Exception:
        logging.exception("send_fn failed")

async def _erc20_meta(w3: Web3, token: str, cache: Dict[str, Tuple[str,int]]) -> Tuple[str,int]:
    token = token.lower()
    if token in cache: return cache[token]
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=[
            {"anonymous":False,"inputs":[{"indexed":True,"name":"from","type":"address"},{"indexed":True,"name":"to","type":"address"},{"indexed":False,"name":"value","type":"uint256"}],"name":"Transfer","type":"event"},
            {"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"stateMutability":"view","type":"function"},
            {"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
        ])
        sym = c.functions.symbol().call()
        dec = c.functions.decimals().call()
        cache[token]=(sym,int(dec))
    except Exception:
        cache[token]=(token[:6].upper(),18)
    return cache[token]

def _looks_like_swap(tx_to: Optional[str], tx_input: Optional[str|bytes], logs: List[LogReceipt]) -> bool:
    if tx_to and tx_to.lower() in KNOWN_ROUTERS: return True
    for lg in logs:
        try:
            t0 = lg["topics"][0].hex() if hasattr(lg["topics"][0],"hex") else str(lg["topics"][0])
            if t0 in (V2_SWAP_TOPIC, V3_SWAP_TOPIC, DEPOSIT_TOPIC, WITHDRAW_TOPIC): return True
        except Exception: pass
    if isinstance(tx_input,(bytes,bytearray)) and len(tx_input)>=4 and ("0x"+tx_input[:4].hex()) in SWAP_SELECTORS: return True
    if isinstance(tx_input,str) and tx_input.startswith("0x") and len(tx_input)>=10 and tx_input[:10] in SWAP_SELECTORS: return True
    return False

def _collect_logs_for_wallet(w3: Web3, wallet_lc: str, start_block: int, end_block: int) -> List[LogReceipt]:
    wl = _wallet_topic(wallet_lc)
    logs: List[LogReceipt] = []
    # ERC20 Transfer (from or to wallet)
    logs += w3.eth.get_logs({"fromBlock":start_block,"toBlock":end_block,"topics":[TRANSFER_TOPIC, wl, None]})
    logs += w3.eth.get_logs({"fromBlock":start_block,"toBlock":end_block,"topics":[TRANSFER_TOPIC, None, wl]})
    # WCRO/WETH9 wrap/unwrap events (Deposit/Withdrawal) with wallet as indexed addr in topic[1]
    logs += w3.eth.get_logs({"fromBlock":start_block,"toBlock":end_block,"topics":[DEPOSIT_TOPIC, wl]})
    logs += w3.eth.get_logs({"fromBlock":start_block,"toBlock":end_block,"topics":[WITHDRAW_TOPIC, wl]})
    return logs

async def _scan_logs_range(w3: Web3, wallet_lc: str, b0: int, b1: int, logger, meta_cache, notify, send_fn):
    logs = []
    try:
        logs = _collect_logs_for_wallet(w3, wallet_lc, b0, b1)
    except Exception as e:
        # απλό throttle αν ο κόμβος “τσινάει”
        await asyncio.sleep(min(BACKOFF_MAX_SEC, 2.0))
        logs = _collect_logs_for_wallet(w3, wallet_lc, b0, b1)

    txhs: set[str] = set()
    for lg in logs:
        try: txhs.add(lg["transactionHash"].hex())
        except Exception: pass

    for txh in txhs:
        try:
            tx = w3.eth.get_transaction(txh)
            rc = w3.eth.get_transaction_receipt(txh)
            blk = w3.eth.get_block(rc["blockNumber"])
            ts_iso = _ts(blk.timestamp)
        except Exception:
            await asyncio.sleep(0.2); continue

        # Native CRO (e.g. send CRO to router / receive CRO from unwrap)
        try:
            if int(tx["value"])>0 and (tx["from"].lower()==wallet_lc or (tx["to"] and tx["to"].lower()==wallet_lc)):
                dirn = "OUT" if tx["from"].lower()==wallet_lc else "IN"
                val  = _to_dec18(int(tx["value"]))
                note = KNOWN_ROUTERS.get((tx["to"] or "").lower(),"") if tx["to"] else ""
                await _send(send_fn, f"💸 {dirn} {val:.6f} CRO{(' via '+note) if note else ''}\nTx: {txh[:10]}…{txh[-8:]}", notify)
                _append_ledger(ts_iso, "CRO", "SELL" if dirn=="OUT" else "BUY", f"{val}", "0", "0", txh)
        except Exception: pass

        # Legs: ERC20 Transfer + WCRO Deposit/Withdrawal
        lines: List[str] = []

        # Handle ERC-20 Transfers
        for lg in rc["logs"]:
            try:
                t0 = lg["topics"][0].hex() if hasattr(lg["topics"][0],"hex") else str(lg["topics"][0])
                if t0 != TRANSFER_TOPIC: continue
                from_addr = _hex_addr(lg["topics"][1]).lower()
                to_addr   = _hex_addr(lg["topics"][2]).lower()
                if wallet_lc not in (from_addr, to_addr): continue
                token = lg["address"]
                sym, dec = await _erc20_meta(w3, token, meta_cache)
                data = lg["data"]; amount = int(str(data),16) if isinstance(data,str) else int.from_bytes(data,"big")
                side = "OUT" if from_addr==wallet_lc else "IN"
                q = _fmt_amt(amount, dec)
                lines.append(f"{side} {q} {sym}")
                _append_ledger(ts_iso, sym, "SELL" if side=="OUT" else "BUY", q, "0", "0", txh)
            except Exception: continue

        # Handle WCRO/WETH9 Deposit/Withdrawal to reflect wrap/unwrap as token legs
        for lg in rc["logs"]:
            try:
                t0 = lg["topics"][0].hex() if hasattr(lg["topics"][0],"hex") else str(lg["topics"][0])
                if t0 not in (DEPOSIT_TOPIC, WITHDRAW_TOPIC): continue
                who = _hex_addr(lg["topics"][1]).lower()
                if who != wallet_lc: continue
                # WCRO contracts έχουν 18 decimals — θα το θεωρήσουμε WCRO symbol
                data = lg["data"]; amount = int(str(data),16) if isinstance(data,str) else int.from_bytes(data,"big")
                q = _fmt_amt(amount, 18)
                if t0 == DEPOSIT_TOPIC:
                    lines.append(f"IN {q} WCRO (wrap CRO)")
                    _append_ledger(ts_iso, "WCRO", "BUY", q, "0", "0", txh)
                else:
                    lines.append(f"OUT {q} WCRO (unwrap)")
                    _append_ledger(ts_iso, "WCRO", "SELL", q, "0", "0", txh)
            except Exception: continue

        if lines:
            is_swap = _looks_like_swap(tx.get("to"), tx.get("input"), rc["logs"])
            head = "🔄 Swap" if is_swap else "🔔 Transfer"
            router_note = ""
            if tx.get("to") and tx["to"].lower() in KNOWN_ROUTERS:
                router_note = f" via {KNOWN_ROUTERS[tx['to'].lower()]}"
            await _send(send_fn, f"{head}{router_note}\n" + "\n".join(f"• {ln}" for ln in lines) + f"\nTx: {txh[:10]}…{txh[-8:]}", notify)

async def monitor_wallet(send_fn, logger=logging.getLogger("realtime")):
    if not WALLET:
        logger.warning("No WALLET_ADDRESS set; realtime monitor disabled.")
        return

    # Sync provider factory (no run_until_complete μέσα στο loop)
    w3 = None
    backoff = 1.0
    while w3 is None:
        try:
            w3 = _make_web3(logger)
        except Exception as e:
            logger.warning("Provider connect failed: %s", e)
            await asyncio.sleep(min(BACKOFF_MAX_SEC, backoff))
            backoff = min(BACKOFF_MAX_SEC, backoff*1.8+0.5)

    wallet = Web3.to_checksum_address(WALLET)
    wl = wallet.lower()
    meta_cache: Dict[str, Tuple[str,int]] = {}

    # ---- Startup backfill (batched ranges) ----
    try:
        latest = w3.eth.get_block("latest").number
        start  = max(0, latest - BACKFILL_BLOCKS)
        logger.info("Backfill %s..%s (chunk=%s, notify=%s)", start, latest, BACKFILL_CHUNK, BACKFILL_NOTIFY)
        for b0 in range(start, latest+1, BACKFILL_CHUNK):
            b1 = min(latest, b0 + BACKFILL_CHUNK - 1)
            try:
                await _scan_logs_range(w3, wl, b0, b1, logger, meta_cache, BACKFILL_NOTIFY, send_fn)
            except Exception as e:
                logger.warning("Backfill chunk %s-%s failed: %s", b0, b1, e)
                await asyncio.sleep(min(BACKOFF_MAX_SEC, 3.0))
        last_handled = latest
    except Exception as e:
        logger.exception("Backfill failed: %s", e)
        last_handled = w3.eth.get_block("latest").number

    # ---- Live loop: per-block getLogs (cheap), με confirmations ----
    while True:
        try:
            latest = w3.eth.get_block("latest").number
            target = latest - CONFIRMATIONS
            if target < last_handled + 1:
                await asyncio.sleep(POLL_INTERVAL); continue

            for b in range(last_handled+1, target+1):
                try:
                    await _scan_logs_range(w3, wl, b, b, logger, meta_cache, True, send_fn)
                except Exception as e:
                    logger.warning("Block %s scan failed: %s", b, e)
                    await asyncio.sleep(min(BACKOFF_MAX_SEC, 2.0))
                last_handled = b

            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.warning("Live loop error: %s — reconnecting…", e)
            w3 = None; backoff = 1.0
            while w3 is None:
                try:
                    w3 = _make_web3(logger)
                except Exception as e2:
                    logger.warning("Reconnect failed: %s", e2)
                    await asyncio.sleep(min(BACKOFF_MAX_SEC, backoff))
                    backoff = min(BACKOFF_MAX_SEC, backoff*1.8+0.5)
