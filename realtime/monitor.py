# realtime/monitor.py
from __future__ import annotations
import os, asyncio, logging, time, csv
from typing import Dict, List, Tuple
from decimal import Decimal as D
from datetime import datetime, timezone
from threading import Lock
from web3 import Web3

# ---- logging ----
log = logging.getLogger("realtime")

# ---- RPC (HTTP; public nodes hate WSS for this use-case) ----
RPC_URLS = [u.strip() for u in (
    os.getenv("CRONOS_HTTPS_URL")
    or os.getenv("CRRONOS_RPC_URL")  # backward-typo guard
    or os.getenv("CRONOS_RPC_URL")
    or "https://evm.cronos.org,https://cronos.blockpi.network/v1/rpc/public,https://cronos-evm-rpc.publicnode.com"
).split(",") if u.strip()]

def _connect() -> Web3:
    last = None
    for url in RPC_URLS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 30}))
            if w3.is_connected():
                log.info("Connected RPC: %s", url)
                return w3
        except Exception as e:
            last = e
            log.warning("RPC failed %s: %s", url, e)
    raise RuntimeError(f"No HTTP RPC available. Last error: {last}")

# ---- Wallet ----
WALLET = (os.getenv("WALLET_ADDRESS") or "").strip().lower()

# ---- Built-in seed token list (from your /scan) + dynamic learn ----
SEED_TOKEN_ADDRS = [
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

from core.discovery import discover_tokens_for_wallet
from core.pricing import get_spot_usd

# WCRO (wrap/unwrap)
WCRO_ADDRS = [a.strip().lower() for a in (os.getenv("WCRO_ADDRESSES","0x5c7f8a570d578ed84e63fdfa7b1ee72deae1ae23").split(",")) if a.strip()]

# ---- Pacing ----
CONFIRMATIONS   = int(os.getenv("RT_CONFIRMS", "0"))
POLL_INTERVAL   = float(os.getenv("RT_POLL_SEC", "1.2"))
SLICE           = int(os.getenv("RT_SLICE", "40"))
BACKFILL_BLOCKS = int(os.getenv("RT_BACKFILL_BLOCKS", "0"))
BACKFILL_NOTIFY = (os.getenv("RT_BACKFILL_NOTIFY", "0").lower() in ("1","true","yes"))
ADDR_REFRESH_SEC= float(os.getenv("ADDR_REFRESH_SEC", "45"))
MAX_RETRY_SLEEP = 12.0

# ---- Topics ----
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
DEPOSIT_TOPIC  = Web3.keccak(text="Deposit(address,uint256)").hex()
WITHDRAW_TOPIC = Web3.keccak(text="Withdrawal(address,uint256)").hex()

# ---- Ledger ----
LEDGER_PATH = os.getenv("LEDGER_CSV", "data/ledger.csv")

def _ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)

def _append_ledger(ts_iso: str, symbol: str, side: str, qty: str, price_usd: str, fee_usd: str, txh: str):
    _ensure_dir(LEDGER_PATH)
    header = not os.path.exists(LEDGER_PATH)
    with open(LEDGER_PATH, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if header:
            w.writerow(["ts","symbol","side","qty","price","fee","tx","chain"])
        w.writerow([ts_iso,symbol,side,qty,price_usd,fee_usd,txh,"cronos"])

# ---- Small utils ----
def _fmt_amt(raw: int, decimals: int) -> str:
    return f"{(D(raw)/(D(10)**decimals)):.6f}".rstrip("0").rstrip(".")

def _ts(block_ts:int)->str:
    return datetime.fromtimestamp(block_ts, tz=timezone.utc).isoformat()

def _wallet_topic(addr_lower: str) -> str:
    return "0x" + addr_lower.replace("0x","").rjust(64, "0")

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

# ---- Token allow-list (dynamic) ----
_TOKEN_SET_LOCK = Lock()
TOKEN_ADDRS_SET = set(ENV_TOKEN_ADDRS or SEED_TOKEN_ADDRS)

def _get_token_addr_list() -> List[str]:
    with _TOKEN_SET_LOCK:
        return list(TOKEN_ADDRS_SET)

def _merge_token_addresses(addrs: List[str]) -> int:
    added = 0
    with _TOKEN_SET_LOCK:
        for a in addrs:
            aa = a.strip().lower()
            if aa.startswith("0x") and len(aa)==42 and aa not in TOKEN_ADDRS_SET:
                TOKEN_ADDRS_SET.add(aa); added += 1
    return added

async def _refresh_addresses_loop(wallet: str, send_fn):
    while True:
        try:
            toks = discover_tokens_for_wallet(wallet) or []
            new_addrs = [t.get("address","").strip().lower() for t in toks if t.get("address")]
            n = _merge_token_addresses(new_addrs)
            if n > 0:
                await _send(send_fn, f"🧠 Learned {n} new token contract(s). Live monitor updated.", True)
        except Exception as e:
            log.debug("addr refresh failed: %s", e)
        await asyncio.sleep(ADDR_REFRESH_SEC)

# ---- Queries ----
def _q_transfer(wallet_topic: str, addr_list: List[str], b0: int, b1: int):
    addrs = [Web3.to_checksum_address(a) for a in addr_list]
    q_from = {"fromBlock": b0, "toBlock": b1, "address": addrs, "topics": [TRANSFER_TOPIC, wallet_topic, None]}
    q_to   = {"fromBlock": b0, "toBlock": b1, "address": addrs, "topics": [TRANSFER_TOPIC, None, wallet_topic]}
    return q_from, q_to

def _q_wrap(wallet_topic: str, b0: int, b1: int):
    addrs = [Web3.to_checksum_address(a) for a in WCRO_ADDRS]
    dep = {"fromBlock": b0, "toBlock": b1, "address": addrs, "topics":[DEPOSIT_TOPIC, wallet_topic]}
    wdr = {"fromBlock": b0, "toBlock": b1, "address": addrs, "topics":[WITHDRAW_TOPIC, wallet_topic]}
    return dep, wdr

def _get_logs_safe(w3: Web3, q: dict, label: str, retry: int = 3) -> List[dict]:
    delay = 0.6
    for i in range(retry):
        try:
            return w3.eth.get_logs(q)
        except Exception as e:
            if i == retry-1:
                log.debug("getLogs %s failed: %s", label, e); return []
            time.sleep(min(MAX_RETRY_SLEEP, delay)); delay *= 1.7
    return []

# ---- Core monitor ----
async def monitor_wallet(send_fn, logger=log):
    if not WALLET:
        await _send(send_fn, "⚠️ WALLET_ADDRESS not set — realtime off.", True); return

    w3 = _connect()
    wl_topic = _wallet_topic(WALLET)
    meta_cache: Dict[str, Tuple[str,int]] = {}

    # learn loop
    asyncio.create_task(_refresh_addresses_loop(WALLET, send_fn))

    async def scan_range(b0: int, b1: int, notify: bool):
        addr_list = _get_token_addr_list()
        logs: List[dict] = []
        q_from, q_to = _q_transfer(wl_topic, addr_list, b0, b1)
        dep, wdr     = _q_wrap(wl_topic, b0, b1)
        logs += _get_logs_safe(w3, q_from, f"from {b0}-{b1}")
        logs += _get_logs_safe(w3, q_to,   f"to {b0}-{b1}")
        logs += _get_logs_safe(w3, dep,    f"wcro-dep {b0}-{b1}")
        logs += _get_logs_safe(w3, wdr,    f"wcro-wdr {b0}-{b1}")

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
                        if (from_addr.lower()!=WALLET) and (to_addr.lower()!=WALLET):
                            continue
                        side = "OUT" if from_addr.lower()==WALLET else "IN"
                        token = lg["address"].lower()
                        sym, dec = _sym_dec(w3, token, meta_cache)
                        amount = int(lg["data"],16) if isinstance(lg["data"], str) else int.from_bytes(lg["data"], "big")
                        qty = _fmt_amt(amount, dec)
                        # snapshot spot price in USD at alert time
                        try:
                            px = get_spot_usd(sym, token_address=token) or 0
                        except Exception:
                            px = 0
                        px_str = f"{D(str(px)):.6f}".rstrip("0").rstrip(".") if px else "0"
                        lines.append(f"{side} {qty} {sym} @ ${px_str}")
                        _append_ledger(ts_iso, sym, "SELL" if side=="OUT" else "BUY", qty, px_str, "0", txh)
                    elif t0 in (DEPOSIT_TOPIC, WITHDRAW_TOPIC):
                        who = "0x" + (lg["topics"][1].hex()[-40:] if hasattr(lg["topics"][1],"hex") else str(lg["topics"][1])[-40:])
                        if who.lower() != WALLET: 
                            continue
                        amount = int(lg["data"],16) if isinstance(lg["data"], str) else int.from_bytes(lg["data"], "big")
                        qty = _fmt_amt(amount, 18)
                        try:
                            # WCRO≈CRO price
                            px = get_spot_usd("CRO", token_address=WCRO_ADDRS[0]) or 0
                        except Exception:
                            px = 0
                        px_str = f"{D(str(px)):.6f}".rstrip("0").rstrip(".") if px else "0"
                        if t0 == DEPOSIT_TOPIC:
                            lines.append(f"IN {qty} WCRO (wrap) @ ${px_str}")
                            _append_ledger(ts_iso, "WCRO", "BUY", qty, px_str, "0", txh)
                        else:
                            lines.append(f"OUT {qty} WCRO (unwrap) @ ${px_str}")
                            _append_ledger(ts_iso, "WCRO", "SELL", qty, px_str, "0", txh)
                except Exception:
                    continue

            if lines:
                await _send(send_fn, "🔄 Swap/Transfer\n" + "\n".join(f"• {ln}" for ln in lines) + f"\nTx: {txh[:10]}…{txh[-8:]}", notify)

    # initial backfill (0 by default)
    try:
        head = w3.eth.block_number
    except Exception:
        w3 = _connect(); head = w3.eth.block_number

    if BACKFILL_BLOCKS > 0:
        start = max(0, head - BACKFILL_BLOCKS)
        log.info("Backfill %s..%s (notify=%s)", start, head, BACKFILL_NOTIFY)
        cur = start
        while cur <= head:
            hi = min(head, cur + SLICE - 1)
            await scan_range(cur, hi, BACKFILL_NOTIFY)
            cur = hi + 1

    # live loop (persistent w3, reconnect on failure)
    last = head
    backoff = 1.0
    while True:
        try:
            head = w3.eth.block_number
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
            backoff = 1.0
            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            log.warning("monitor error (reconnect): %s", e)
            await asyncio.sleep(min(MAX_RETRY_SLEEP, backoff))
            backoff = min(MAX_RETRY_SLEEP, backoff*1.7 + 0.5)
            try:
                w3 = _connect()
            except Exception as e2:
                log.warning("reconnect failed: %s", e2)
