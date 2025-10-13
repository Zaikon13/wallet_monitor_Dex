# realtime/monitor.py
from __future__ import annotations
import os, asyncio, logging
from typing import Dict, List, Tuple, Optional
from decimal import Decimal
from datetime import datetime, timezone
from web3 import Web3

WALLET = (os.getenv("WALLET_ADDRESS") or "").strip().lower()
# Use HTTP RPC; avoid WSS rate limits on public nodes
HTTPS_URLS = [u.strip() for u in (
    os.getenv("CRONOS_HTTPS_URL")
    or os.getenv("CRONOS_RPC_URL")
    or "https://evm.cronos.org"
).split(",") if u.strip()]

# Comma-separated token contracts to monitor (MUST include the tokens you trade)
# e.g. ADA, USDT, USDC, WCRO, WETH, WBTC, XRP, SOL, SUI, HBAR, XYO, TRUMP...
MONITOR_TOKEN_ADDRESSES = [
    a.strip().lower() for a in (os.getenv("MONITOR_TOKEN_ADDRESSES","").split(",")) if a.strip()
]

# Known WCRO contract(s) for wrap/unwrap (Deposit/Withdrawal). Add if different on Cronos:
WCRO_ADDRESSES = [a.strip().lower() for a in (os.getenv("WCRO_ADDRESSES","0x5c7f8a570d578ed84e63fdfa7b1ee72deae1ae23").split(",")) if a.strip()]

CONFIRMATIONS   = int(os.getenv("RT_CONFIRMS", "0"))
POLL_INTERVAL   = float(os.getenv("RT_POLL_SEC", "1.2"))
LEDGER_PATH     = os.getenv("LEDGER_CSV", "data/ledger.csv")
# Backfill is dangerous on public RPC -> keep 0 or tiny
BACKFILL_BLOCKS = int(os.getenv("RT_BACKFILL_BLOCKS", "0"))
BACKFILL_NOTIFY = (os.getenv("RT_BACKFILL_NOTIFY", "0").lower() in ("1","true","yes"))

# Topics
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
DEPOSIT_TOPIC  = Web3.keccak(text="Deposit(address,uint256)").hex()
WITHDRAW_TOPIC = Web3.keccak(text="Withdrawal(address,uint256)").hex()

def _make_web3(logger) -> Web3:
    last_ex = None
    for url in HTTPS_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                logger.info("Connected RPC: %s", url)
                return w3
        except Exception as e:
            last_ex = e
            logger.warning("RPC failed %s: %s", url, e)
    raise RuntimeError(f"No RPC available. Last: {last_ex}")

def _wallet_topic(addr_lower: str) -> str:
    return "0x" + addr_lower.replace("0x","").rjust(64, "0")

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

from decimal import Decimal as D
def _fmt_amt(raw: int, decimals: int) -> str:
    return f"{(D(raw)/(D(10)**decimals)):.6f}".rstrip("0").rstrip(".")

def _ts(block_ts:int)->str:
    return datetime.fromtimestamp(block_ts, tz=timezone.utc).isoformat()

def _sym_dec(w3: Web3, token: str, cache: Dict[str, Tuple[str,int]]) -> Tuple[str,int]:
    token = token.lower()
    if token in cache: return cache[token]
    abi = [
        {"anonymous":False,"inputs":[{"indexed":True,"name":"from","type":"address"},{"indexed":True,"name":"to","type":"address"},{"indexed":False,"name":"value","type":"uint256"}],"name":"Transfer","type":"event"},
        {"inputs":[],"name":"symbol","outputs":[{"name":"","type":"string"}],"stateMutability":"view","type":"function"},
        {"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"stateMutability":"view","type":"function"},
    ]
    try:
        c = w3.eth.contract(address=Web3.to_checksum_address(token), abi=abi)
        sym = c.functions.symbol().call()
        dec = int(c.functions.decimals().call())
        cache[token]=(sym,dec)
    except Exception:
        cache[token]=(token[:6].upper(),18)
    return cache[token]

async def _send(send_fn, text: str, notify=True):
    if not notify: return
    try:
        if asyncio.iscoroutinefunction(send_fn): await send_fn(text)
        else: send_fn(text)
    except Exception:
        logging.exception("send_fn failed")

def _build_transfer_queries(wallet_topic: str, address_list: List[str], start_block: int, end_block: int):
    # We must provide "address": [...] to satisfy publicnode restriction.
    # Capture both directions: from=wallet OR to=wallet
    q_from = {
        "fromBlock": start_block, "toBlock": end_block,
        "address": [Web3.to_checksum_address(a) for a in address_list],
        "topics": [TRANSFER_TOPIC, wallet_topic, None]
    }
    q_to = {
        "fromBlock": start_block, "toBlock": end_block,
        "address": [Web3.to_checksum_address(a) for a in address_list],
        "topics": [TRANSFER_TOPIC, None, wallet_topic]
    }
    return q_from, q_to

def _build_wrap_queries(wallet_topic: str, start_block: int, end_block: int):
    # WCRO wrap/unwrap happen on WCRO contracts; address must be specific WCRO address(es)
    addr = [Web3.to_checksum_address(a) for a in WCRO_ADDRESSES]
    q_dep = {"fromBlock": start_block, "toBlock": end_block, "address": addr, "topics":[DEPOSIT_TOPIC, wallet_topic]}
    q_wdr = {"fromBlock": start_block, "toBlock": end_block, "address": addr, "topics":[WITHDRAW_TOPIC, wallet_topic]}
    return q_dep, q_wdr

async def monitor_wallet(send_fn, logger=logging.getLogger("realtime")):
    if not WALLET:
        logger.warning("No WALLET_ADDRESS set; realtime monitor disabled.")
        return
    if not MONITOR_TOKEN_ADDRESSES:
        logger.error("MONITOR_TOKEN_ADDRESSES is empty — add your token contracts (e.g. ADA, USDT, ...).")
        await _send(send_fn, "⚠️ Set MONITOR_TOKEN_ADDRESSES in ENV (comma-separated CRC20 contracts).", True)
        return

    w3 = _make_web3(logger)
    wl_topic = _wallet_topic(WALLET)

    # Backfill (optional/tiny)
    latest = w3.eth.block_number
    start  = max(0, latest - BACKFILL_BLOCKS) if BACKFILL_BLOCKS>0 else latest
    logger.info("Backfill %s..%s (notify=%s)", start, latest, BACKFILL_NOTIFY)

    meta_cache: Dict[str, Tuple[str,int]] = {}

    async def scan_range(b0: int, b1: int, notify: bool):
        # Transfers for monitored token addresses
        q_from, q_to = _build_transfer_queries(wl_topic, MONITOR_TOKEN_ADDRESSES, b0, b1)
        logs = []
        try:
            logs += w3.eth.get_logs(q_from)
        except Exception as e:
            logger.warning("getLogs(from) %s-%s failed: %s", b0, b1, e)
        try:
            logs += w3.eth.get_logs(q_to)
        except Exception as e:
            logger.warning("getLogs(to) %s-%s failed: %s", b0, b1, e)
        # Wrap/unwrap on WCRO
        q_dep, q_wdr = _build_wrap_queries(wl_topic, b0, b1)
        try:
            logs += w3.eth.get_logs(q_dep)
        except Exception as e:
            logger.warning("getLogs(deposit) %s-%s failed: %s", b0, b1, e)
        try:
            logs += w3.eth.get_logs(q_wdr)
        except Exception as e:
            logger.warning("getLogs(withdraw) %s-%s failed: %s", b0, b1, e)

        # Group by tx
        by_tx: Dict[str, List[dict]] = {}
        for lg in logs:
            txh = lg["transactionHash"].hex()
            by_tx.setdefault(txh, []).append(lg)

        # Emit
        for txh, items in by_tx.items():
            # Fetch block ts once
            try:
                rc = w3.eth.get_transaction_receipt(txh)
                blk = w3.eth.get_block(rc["blockNumber"])
                ts_iso = _ts(blk.timestamp)
            except Exception:
                ts_iso = datetime.now(timezone.utc).isoformat()

            lines: List[str] = []
            for lg in items:
                try:
                    t0 = lg["topics"][0].hex() if hasattr(lg["topics"][0],"hex") else str(lg["topics"][0])
                    if t0 == TRANSFER_TOPIC:
                        from_addr = "0x" + (lg["topics"][1].hex()[-40:] if hasattr(lg["topics"][1],"hex") else str(lg["topics"][1])[-40:])
                        to_addr   = "0x" + (lg["topics"][2].hex()[-40:] if hasattr(lg["topics"][2],"hex") else str(lg["topics"][2])[-40:])
                        side = "OUT" if from_addr.lower()==WALLET else "IN"
                        token = lg["address"].lower()
                        sym, dec = _sym_dec(w3, token, meta_cache)
                        amount = int(lg["data"],16) if isinstance(lg["data"], str) else int.from_bytes(lg["data"], "big")
                        qty = _fmt_amt(amount, dec)
                        lines.append(f"{side} {qty} {sym}")
                        _append_ledger(ts_iso, sym, "SELL" if side=="OUT" else "BUY", qty, "0", "0", txh)
                    elif t0 in (DEPOSIT_TOPIC, WITHDRAW_TOPIC):
                        who = "0x" + (lg["topics"][1].hex()[-40:] if hasattr(lg["topics"][1],"hex") else str(lg["topics"][1])[-40:])
                        if who.lower() != WALLET: 
                            continue
                        amount = int(lg["data"],16) if isinstance(lg["data"], str) else int.from_bytes(lg["data"], "big")
                        qty = _fmt_amt(amount, 18)
                        if t0 == DEPOSIT_TOPIC:
                            lines.append(f"IN {qty} WCRO (wrap CRO)")
                            _append_ledger(ts_iso, "WCRO", "BUY", qty, "0", "0", txh)
                        else:
                            lines.append(f"OUT {qty} WCRO (unwrap)")
                            _append_ledger(ts_iso, "WCRO", "SELL", qty, "0", "0", txh)
                except Exception:
                    continue

            if lines:
                await _send(send_fn, "🔄 Swap/Transfer\n" + "\n".join(f"• {ln}" for ln in lines) + f"\nTx: {txh[:10]}…{txh[-8:]}", notify)

    # Initial scan (usually latest or tiny backfill)
    if start <= latest:
        await scan_range(start, latest, BACKFILL_NOTIFY)

    # Live loop (incremental ranges)
    last = latest
    while True:
        try:
            head = w3.eth.block_number
            target = head - CONFIRMATIONS
            if target > last:
                # Scan in tiny slices to stay under rate limits
                b0 = last + 1
                b1 = target
                # safety cap slice size
                step = int(os.getenv("RT_SLICE", "40"))
                cur = b0
                while cur <= b1:
                    hi = min(b1, cur + step - 1)
                    await scan_range(cur, hi, True)
                    cur = hi + 1
                last = b1
            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logging.error("monitor_wallet crashed: %s", e)
            # small backoff then reconnect provider
            await asyncio.sleep(3)
            w3 = _make_web3(logger)
