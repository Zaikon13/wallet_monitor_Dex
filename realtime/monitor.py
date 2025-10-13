# realtime/monitor.py
from __future__ import annotations
import os, asyncio, logging
from typing import Dict, List, Tuple, Optional
from decimal import Decimal as D
from datetime import datetime, timezone
from web3 import Web3

log = logging.getLogger("realtime")

# ---------- RPC (HTTP μόνο – public nodes δεν αντέχουν WSS) ----------
HTTPS_URLS = [u.strip() for u in (
    os.getenv("CRONOS_HTTPS_URL")
    or os.getenv("CRONOS_RPC_URL")
    or "https://evm.cronos.org,https://cronos.blockpi.network/v1/rpc/public"
).split(",") if u.strip()]

def _make_web3() -> Web3:
    last = None
    for url in HTTPS_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                log.info("Connected RPC: %s", url)
                return w3
        except Exception as e:
            last = e
            log.warning("RPC failed %s: %s", url, e)
    raise RuntimeError(f"No HTTP RPC available. Last error: {last}")

# ---------- Wallet ----------
WALLET = (os.getenv("WALLET_ADDRESS") or "").strip().lower()
if not WALLET:
    log.warning("WALLET_ADDRESS not set; realtime disabled")

# ---------- Tokens (fallback defaults αν δεν δωθεί ENV) ----------
# ΠΡΟΣΟΧΗ: Αυτές οι διευθύνσεις είναι από τα δικά σου logs (/scan).
DEFAULT_TOKEN_ADDRS = [
    "0x0e517979c2c1c1522ddb0c73905e0d39b3f990c0",  # ADA
    "0x66e428c3f67a68878562e79a0234c1f83c208770",  # USDT
    "0xc21223249ca28397b4b6541dffaecc539bff0c59",  # USDC
    "0xe44fd7fcb2b1581822d0c862b68222998a0c299a",  # WETH
    "0x062e66477faf219f25d27dced647bf57c3107d52",  # WBTC
    "0xb9ce0dd29c91e02d4620f57a66700fc5e41d6d15",  # XRP
    "0xc9de0f3e08162312528ff72559db82590b481800",  # SOL
    "0x81710203a7fc16797ac9899228a87fd622df9706",  # SUI
    "0xe0c7226a58f54db71edc6289ba2dc80349b41974",  # HBAR
    "0x211153266f15f9314b214a7dd614d90f850a8d6a",  # XYO
    "0xd1d7a0ff6cd3d494038b7fb93dbaef624da6f417",  # TRUMP
    "0xa46d5775c18837e380efb3d8bf9d315bcd028ab1",  # CBO
    "0xf78a326acd53651f8df5d8b137295e434b7c8ba5",  # MATIC
]
ENV_TOKEN_ADDRS = [a.strip().lower() for a in (os.getenv("MONITOR_TOKEN_ADDRESSES","").split(",")) if a.strip()]
TOKEN_ADDRS = (ENV_TOKEN_ADDRS or DEFAULT_TOKEN_ADDRS)
if not ENV_TOKEN_ADDRS:
    log.info("Using built-in DEFAULT_TOKEN_ADDRS (%d tokens)", len(TOKEN_ADDRS))

# WCRO για wrap/unwrap
WCRO_ADDRESSES = [a.strip().lower() for a in (os.getenv("WCRO_ADDRESSES","0x5c7f8a570d578ed84e63fdfa7b1ee72deae1ae23").split(",")) if a.strip()]

# ---------- Pacing ----------
CONFIRMATIONS   = int(os.getenv("RT_CONFIRMS", "0"))
POLL_INTERVAL   = float(os.getenv("RT_POLL_SEC", "1.2"))
SLICE           = int(os.getenv("RT_SLICE", "40"))
BACKFILL_BLOCKS = int(os.getenv("RT_BACKFILL_BLOCKS", "0"))  # άφησέ το 0 σε public RPC
BACKFILL_NOTIFY = (os.getenv("RT_BACKFILL_NOTIFY", "0").lower() in ("1","true","yes"))

# ---------- Topics ----------
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
DEPOSIT_TOPIC  = Web3.keccak(text="Deposit(address,uint256)").hex()
WITHDRAW_TOPIC = Web3.keccak(text="Withdrawal(address,uint256)").hex()

# ---------- Small utils ----------
def _wallet_topic(addr_lower: str) -> str:
    return "0x" + addr_lower.replace("0x","").rjust(64, "0")

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

LEDGER_PATH = os.getenv("LEDGER_CSV", "data/ledger.csv")
def _append_ledger(ts_iso: str, symbol: str, side: str, qty: str, price: str, fee: str, txh: str):
    _ensure_dir(LEDGER_PATH)
    header = not os.path.exists(LEDGER_PATH)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        if header:
            f.write("ts,symbol,side,qty,price,fee,tx,chain\n")
        f.write(f"{ts_iso},{symbol},{side},{qty},{price},{fee},{txh},cronos\n")

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
        log.exception("send_fn failed")

def _build_transfer_queries(wallet_topic: str, address_list: List[str], b0: int, b1: int):
    addr = [Web3.to_checksum_address(a) for a in address_list]
    q_from = {"fromBlock": b0, "toBlock": b1, "address": addr, "topics": [TRANSFER_TOPIC, wallet_topic, None]}
    q_to   = {"fromBlock": b0, "toBlock": b1, "address": addr, "topics": [TRANSFER_TOPIC, None, wallet_topic]}
    return q_from, q_to

def _build_wrap_queries(wallet_topic: str, b0: int, b1: int):
    addr = [Web3.to_checksum_address(a) for a in WCRO_ADDRESSES]
    q_dep = {"fromBlock": b0, "toBlock": b1, "address": addr, "topics":[DEPOSIT_TOPIC, wallet_topic]}
    q_wdr = {"fromBlock": b0, "toBlock": b1, "address": addr, "topics":[WITHDRAW_TOPIC, wallet_topic]}
    return q_dep, q_wdr

# ---------- Main monitor ----------
async def monitor_wallet(send_fn, logger=log):
    if not WALLET:
        await _send(send_fn, "⚠️ WALLET_ADDRESS not set — realtime off.", True); return

    w3 = _make_web3()
    wl_topic = _wallet_topic(WALLET)

    meta_cache: Dict[str, Tuple[str,int]] = {}

    async def scan_range(b0: int, b1: int, notify: bool):
        logs = []
        # Transfers (IN & OUT) για τα monitored tokens
        try: logs += w3.eth.get_logs(_build_transfer_queries(wl_topic, TOKEN_ADDRS, b0, b1)[0])
        except Exception as e: logger.debug("getLogs from %s-%s: %s", b0, b1, e)
        try: logs += w3.eth.get_logs(_build_transfer_queries(wl_topic, TOKEN_ADDRS, b0, b1)[1])
        except Exception as e: logger.debug("getLogs to   %s-%s: %s", b0, b1, e)
        # WCRO wrap/unwrap
        dep_q, wdr_q = _build_wrap_queries(wl_topic, b0, b1)
        try: logs += w3.eth.get_logs(dep_q)
        except Exception as e: logger.debug("getLogs wcro dep %s-%s: %s", b0, b1, e)
        try: logs += w3.eth.get_logs(wdr_q)
        except Exception as e: logger.debug("getLogs wcro wdr %s-%s: %s", b0, b1, e)

        # group by tx
        by_tx: Dict[str, List[dict]] = {}
        for lg in logs:
            txh = lg["transactionHash"].hex()
            by_tx.setdefault(txh, []).append(lg)

        for txh, items in by_tx.items():
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

    # --------- Initial (tiny) backfill ---------
    head = _make_web3().eth.block_number
    if BACKFILL_BLOCKS > 0:
        start = max(0, head - BACKFILL_BLOCKS)
        log.info("Backfill %s..%s (notify=%s)", start, head, BACKFILL_NOTIFY)
        cur = start
        while cur <= head:
            hi = min(head, cur + SLICE - 1)
            await scan_range(cur, hi, BACKFILL_NOTIFY)
            cur = hi + 1

    # --------- Live loop ---------
    last = head
    while True:
        try:
            head = _make_web3().eth.block_number
            target = head - CONFIRMATIONS
            if target > last:
                b0 = last + 1
                b1 = target
                cur = b0
                while cur <= b1:
                    hi = min(b1, cur + SLICE - 1)
                    await scan_range(cur, hi, True)
                    cur = hi + 1
                last = b1
            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            log.warning("monitor loop error: %s", e)
            await asyncio.sleep(2.0)
