# realtime/monitor.py
from __future__ import annotations
import os, asyncio, logging, random
from typing import Dict, List, Tuple, Optional, Any
from decimal import Decimal
from datetime import datetime, timezone
from web3 import Web3
from web3.types import LogReceipt

WALLET = (os.getenv("WALLET_ADDRESS") or "").strip()

# Endpoints (WSS optional, HTTPS τιμά και CRONOS_RPC_URL)
WSS_URLS = [u.strip() for u in (os.getenv("CRONOS_WSS_URL", "").split(",")) if u.strip()]
_https_env = os.getenv("CRONOS_HTTPS_URL") or os.getenv("CRONOS_RPC_URL") or "https://cronos-evm-rpc.publicnode.com"
HTTPS_URLS = [u.strip() for u in _https_env.split(",") if u.strip()]

CONFIRMATIONS     = int(os.getenv("RT_CONFIRMS", "0"))
POLL_INTERVAL     = float(os.getenv("RT_POLL_SEC", "1.0"))
LEDGER_PATH       = os.getenv("LEDGER_CSV", "data/ledger.csv")

# Backfill: Κράτα το μικρό με public RPC
BACKFILL_BLOCKS   = int(os.getenv("RT_BACKFILL_BLOCKS", "800"))
BACKFILL_NOTIFY   = (os.getenv("RT_BACKFILL_NOTIFY", "0").strip() in ("1","true","yes"))
BACKOFF_MAX_SEC   = float(os.getenv("RT_BACKOFF_MAX_SEC", "20"))

# Routers στο Cronos
KNOWN_ROUTERS: Dict[str, str] = {
    "0x145863eb42cf62847a6ca784e6416c1682b1b2ae": "VVS Router",
    "0xe0137ee596c35bf7adedad1e2fd25da595d1e05b": "VVS Router (alt)",
    "0x22d710931f01c1681ca1570ff016ed42eb7b7c2a": "MMF Router",
    "0x76c930c6a2c2d7ee1e2fcfef05b0bb2e6902a84a": "Odos Router",
    "0xdef1abe32c034e558cdd535791643c58a13acc10": "Router (agg)",
}
KNOWN_ROUTERS = {k.lower(): v for k, v in KNOWN_ROUTERS.items()}

# Topics για parsing logs
TRANSFER_TOPIC = Web3.keccak(text="Transfer(address,address,uint256)").hex()
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
            w3 = Web3(Web3.WebsocketProvider(url, websocket_timeout=45)) if kind=="wss" \
                 else Web3(Web3.HTTPProvider(url, request_kwargs={"timeout":45}))
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

def _tx_touches_wallet_or_router(tx, wl: str) -> bool:
    to_l = (tx["to"] or "").lower() if tx.get("to") else ""
    frm  = (tx["from"] or "").lower()
    if frm == wl or to_l == wl:                     # direct in/out
        return True
    if frm == wl and to_l in KNOWN_ROUTERS:         # swap send to router
        return True
    return False

def _parse_receipt_for_wallet(w3: Web3, rc, wl: str, ts_iso: str, txh: str, meta_cache, lines_out: List[str]):
    # ERC-20 Transfers που ακουμπάνε το wallet
    for lg in rc["logs"]:
        try:
            topic0 = lg["topics"][0].hex() if hasattr(lg["topics"][0],"hex") else str(lg["topics"][0])
            if topic0 == TRANSFER_TOPIC:
                from_addr = _hex_addr(lg["topics"][1]).lower()
                to_addr   = _hex_addr(lg["topics"][2]).lower()
                if wl not in (from_addr, to_addr): 
                    continue
                token = lg["address"]
                sym, dec = asyncio.get_event_loop().run_until_complete(_erc20_meta(w3, token, meta_cache))  # safe: small sync call
                data = lg["data"]
                amount = int(str(data),16) if isinstance(data,str) else int.from_bytes(data,"big")
                side = "OUT" if from_addr == wl else "IN"
                q = _fmt_amt(amount, dec)
                lines_out.append(f"{side} {q} {sym}")
                _append_ledger(ts_iso, sym, "SELL" if side=="OUT" else "BUY", q, "0", "0", txh)
            elif topic0 in (DEPOSIT_TOPIC, WITHDRAW_TOPIC):
                who = _hex_addr(lg["topics"][1]).lower()
                if who != wl: 
                    continue
                data = lg["data"]; amount = int(str(data),16) if isinstance(data,str) else int.from_bytes(data,"big")
                q = _fmt_amt(amount, 18)
                if topic0 == DEPOSIT_TOPIC:
                    lines_out.append(f"IN {q} WCRO (wrap CRO)")
                    _append_ledger(ts_iso, "WCRO", "BUY", q, "0", "0", txh)
                else:
                    lines_out.append(f"OUT {q} WCRO (unwrap)")
                    _append_ledger(ts_iso, "WCRO", "SELL", q, "0", "0", txh)
        except Exception:
            continue

async def monitor_wallet(send_fn, logger=logging.getLogger("realtime")):
    if not WALLET:
        logger.warning("No WALLET_ADDRESS set; realtime monitor disabled.")
        return

    # Connect RPC
    w3 = None; backoff=1.0
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

    # ---- Backfill (per-block, χωρίς getLogs) ----
    try:
        latest = w3.eth.get_block("latest").number
        start  = max(0, latest - BACKFILL_BLOCKS)
        logger.info("Backfill %s..%s (notify=%s)", start, latest, BACKFILL_NOTIFY)
        for b in range(start, latest+1):
            blk = w3.eth.get_block(b, full_transactions=True)
            ts_iso = _ts(blk.timestamp)
            for tx in blk.transactions:
                try:
                    if not _tx_touches_wallet_or_router(tx, wl):
                        continue
                    rc = w3.eth.get_transaction_receipt(tx["hash"])
                    txh = tx["hash"].hex()
                    # Native CRO leg
                    if int(tx["value"])>0 and (tx["from"].lower()==wl or (tx["to"] and tx["to"].lower()==wl)):
                        dirn = "OUT" if tx["from"].lower()==wl else "IN"
                        val  = _to_dec18(int(tx["value"]))
                        note = KNOWN_ROUTERS.get((tx["to"] or "").lower(),"") if tx["to"] else ""
                        await _send(send_fn, f"💸 {dirn} {val:.6f} CRO{(' via '+note) if note else ''}\nTx: {txh[:10]}…{txh[-8:]}", BACKFILL_NOTIFY)
                        _append_ledger(ts_iso, "CRO", "SELL" if dirn=="OUT" else "BUY", f"{val}", "0", "0", txh)

                    lines: List[str] = []
                    _parse_receipt_for_wallet(w3, rc, wl, ts_iso, txh, meta_cache, lines)
                    if lines:
                        head = "🔄 Swap" if (tx.get("to") and (tx["to"].lower() in KNOWN_ROUTERS)) else "🔔 Transfer"
                        router_note = ""
                        if tx.get("to") and tx["to"].lower() in KNOWN_ROUTERS:
                            router_note = f" via {KNOWN_ROUTERS[tx['to'].lower()]}"
                        await _send(send_fn, f"{head}{router_note}\n" + "\n".join(f"• {ln}" for ln in lines) + f"\nTx: {txh[:10]}…{txh[-8:]}", BACKFILL_NOTIFY)
                except Exception:
                    continue
        last_handled = latest
    except Exception as e:
        logger.exception("Backfill failed: %s", e)
        last_handled = w3.eth.get_block("latest").number

    # ---- Live loop (per-block) ----
    while True:
        try:
            latest = w3.eth.get_block("latest").number
            target = latest - CONFIRMATIONS
            if target < last_handled + 1:
                await asyncio.sleep(POLL_INTERVAL); continue

            for b in range(last_handled + 1, target + 1):
                blk = w3.eth.get_block(b, full_transactions=True)
                ts_iso = _ts(blk.timestamp)
                for tx in blk.transactions:
                    try:
                        if not _tx_touches_wallet_or_router(tx, wl):
                            continue
                        rc = w3.eth.get_transaction_receipt(tx["hash"])
                        txh = tx["hash"].hex()

                        if int(tx["value"])>0 and (tx["from"].lower()==wl or (tx["to"] and tx["to"].lower()==wl)):
                            dirn = "OUT" if tx["from"].lower()==wl else "IN"
                            val  = _to_dec18(int(tx["value"]))
                            note = KNOWN_ROUTERS.get((tx["to"] or "").lower(),"") if tx["to"] else ""
                            await _send(send_fn, f"💸 {dirn} {val:.6f} CRO{(' via '+note) if note else ''}\nTx: {txh[:10]}…{txh[-8:]}")
                            _append_ledger(ts_iso, "CRO", "SELL" if dirn=="OUT" else "BUY", f"{val}", "0", "0", txh)

                        lines: List[str] = []
                        _parse_receipt_for_wallet(w3, rc, wl, ts_iso, txh, meta_cache, lines)
                        if lines:
                            head = "🔄 Swap" if (tx.get("to") and (tx["to"].lower() in KNOWN_ROUTERS)) else "🔔 Transfer"
                            router_note = ""
                            if tx.get("to") and tx["to"].lower() in KNOWN_ROUTERS:
                                router_note = f" via {KNOWN_ROUTERS[tx['to'].lower()]}"
                            await _send(send_fn, f"{head}{router_note}\n" + "\n".join(f"• {ln}" for ln in lines) + f"\nTx: {txh[:10]}…{txh[-8:]}")
                    except Exception:
                        continue

                last_handled = b
            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.warning("Live loop error: %s — reconnecting…", e)
            w3 = None; backoff=1.0
            while w3 is None:
                try:
                    w3 = _make_web3(logger)
                except Exception as e2:
                    logger.warning("Reconnect failed: %s", e2)
                    await asyncio.sleep(min(BACKOFF_MAX_SEC, backoff))
                    backoff = min(BACKOFF_MAX_SEC, backoff*1.8+0.5)
