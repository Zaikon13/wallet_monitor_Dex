# realtime/monitor.py
# Cronos DeFi Sentinel — full live wallet monitor (CRC20 + native CRO)
from __future__ import annotations

import os
import csv
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, getcontext
from typing import Any, Dict, List, Optional, Set

import aiohttp

try:
    from web3 import Web3
except Exception:
    Web3 = None  # optional

try:
    from core.pricing import get_spot_usd
except Exception:
    def get_spot_usd(symbol: str, token_address: Optional[str] = None) -> Optional[Decimal]:
        return None

# -----------------------------
# ENV / defaults
# -----------------------------
TZ = os.getenv("TZ", "Europe/Athens")
LEDGER_CSV = os.getenv("LEDGER_CSV", "data/ledger.csv")
WALLET = (os.getenv("WALLET_ADDRESS") or "").strip()
WALLET_LC = WALLET.lower() if WALLET else ""

EXPLORER_BASE = os.getenv("CRONOS_EXPLORER_API_BASE", "https://cronos.org/explorer/api").rstrip("/")
EXPLORER_KEY = os.getenv("CRONOS_EXPLORER_API_KEY", "").strip()
RPC_URL = os.getenv("CRONOS_RPC_URL", "https://cronos-evm-rpc.publicnode.com").split(",")[0].strip()

MONITOR_POLL = float(os.getenv("MONITOR_POLL_SECONDS", os.getenv("RT_POLL_SEC", "4")))
getcontext().prec = 36

CSV_FIELDS = ["ts", "symbol", "qty", "side", "price_usd", "tx"]

# -----------------------------
# CSV writer
# -----------------------------
def _ensure_dir():
    base = os.path.dirname(LEDGER_CSV)
    if base:
        os.makedirs(base, exist_ok=True)

def _append_csv(ts, sym, qty, side, px, tx):
    _ensure_dir()
    exists = os.path.isfile(LEDGER_CSV)
    try:
        with open(LEDGER_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if not exists:
                w.writeheader()
            w.writerow({
                "ts": ts,
                "symbol": sym,
                "qty": str(qty),
                "side": side,
                "price_usd": str(px),
                "tx": tx,
            })
    except Exception as e:
        logging.getLogger("realtime").exception(f"write ledger: {e}")

# -----------------------------
# helpers
# -----------------------------
def _dec(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")

def _fmt_qty(q: Decimal) -> str:
    try:
        return f"{q:,.6f}"
    except Exception:
        return str(q)

def _fmt_price(p: Decimal) -> str:
    try:
        if p >= 1000:
            return f"{p:,.2f}"
        if p >= 1:
            return f"{p:.2f}"
        if p >= 0.01:
            return f"{p:.6f}"
        return f"{p:.8f}".rstrip("0").rstrip(".")
    except Exception:
        return str(p)

async def _seed_block(logger):
    if not RPC_URL or not Web3:
        return None
    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        blk = int(w3.eth.block_number)
        logger.info(f"seed current block {blk}")
        return blk
    except Exception as e:
        logger.warning(f"seed block failed: {e}")
        return None

# -----------------------------
# Explorer fetchers
# -----------------------------
async def _fetch(session, params, logger) -> list:
    url = f"{EXPLORER_BASE}/"
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            j = await r.json()
            result = j.get("result")
            if not result or isinstance(result, str):
                return []
            return result
    except Exception as e:
        logger.warning(f"fetch {params.get('action')} failed: {e}")
        return []

async def _get_token_txs(session, wallet, startblock, logger):
    p = {"module": "account", "action": "tokentx", "address": wallet, "sort": "asc"}
    if startblock:
        p["startblock"] = str(startblock + 1)
    if EXPLORER_KEY:
        p["apikey"] = EXPLORER_KEY
    return await _fetch(session, p, logger)

async def _get_native_txs(session, wallet, startblock, logger):
    p = {"module": "account", "action": "txlist", "address": wallet, "sort": "asc"}
    if startblock:
        p["startblock"] = str(startblock + 1)
    if EXPLORER_KEY:
        p["apikey"] = EXPLORER_KEY
    return await _fetch(session, p, logger)

# -----------------------------
# Core parsers
# -----------------------------
def _parse_token_row(r) -> dict:
    blk = int(r.get("blockNumber") or 0)
    ts = datetime.fromtimestamp(int(r.get("timeStamp") or 0), tz=ZoneInfo(TZ)).isoformat(timespec="seconds")
    sym = (r.get("tokenSymbol") or "").upper() or "TOKEN"
    dec = int(r.get("tokenDecimal") or 18)
    qty = _dec(r.get("value")) / (Decimal(10) ** dec)
    frm, to = (r.get("from") or "").lower(), (r.get("to") or "").lower()
    side = "IN" if to == WALLET_LC else ("OUT" if frm == WALLET_LC else "NA")
    tx = r.get("hash") or ""
    uniq = f"TOK:{tx}:{sym}:{qty}:{side}"
    return dict(block=blk, ts=ts, symbol=sym, qty=qty, side=side, tx=tx, uniq=uniq, contract=r.get("contractAddress"))

def _parse_native_row(r) -> dict:
    blk = int(r.get("blockNumber") or 0)
    ts = datetime.fromtimestamp(int(r.get("timeStamp") or 0), tz=ZoneInfo(TZ)).isoformat(timespec="seconds")
    val = _dec(r.get("value")) / Decimal(10**18)
    frm, to = (r.get("from") or "").lower(), (r.get("to") or "").lower()
    side = "IN" if to == WALLET_LC else ("OUT" if frm == WALLET_LC else "NA")
    tx = r.get("hash") or ""
    uniq = f"CRO:{tx}:{val}:{side}"
    return dict(block=blk, ts=ts, symbol="CRO", qty=val, side=side, tx=tx, uniq=uniq)

def _fmt_alert(events: list) -> str:
    lines = []
    for e in events:
        emoji = "🟢 IN" if e["side"] == "IN" else ("🔴 OUT" if e["side"] == "OUT" else "•")
        lines.append(f"• {e['ts'][-8:]} — {emoji} {e['symbol']} {_fmt_qty(e['qty'])} @ ${_fmt_price(e['price_usd'])}")
    return "\n".join(lines)

def _get_price(sym, contract=None):
    try:
        p = get_spot_usd(sym, token_address=contract)
        return _dec(p) if p is not None else Decimal("0")
    except Exception:
        return Decimal("0")

# -----------------------------
# Main loop
# -----------------------------
async def monitor_wallet(send_fn, logger=None):
    if not WALLET:
        raise RuntimeError("WALLET_ADDRESS missing")

    logger = logger or logging.getLogger("realtime")
    logger.setLevel(logging.INFO)
    logger.info(f"Realtime monitor: explorer mode (poll {MONITOR_POLL}s)")

    last_blk = await _seed_block(logger)
    seen: Set[str] = set()

    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        while True:
            try:
                # CRC20 token txs
                token_rows = await _get_token_txs(sess, WALLET, last_blk, logger)
                native_rows = await _get_native_txs(sess, WALLET, last_blk, logger)
                rows = []
                for r in token_rows:
                    rows.append(_parse_token_row(r))
                for r in native_rows:
                    rows.append(_parse_native_row(r))
                if not rows:
                    await asyncio.sleep(MONITOR_POLL)
                    continue

                max_blk = last_blk or 0
                evs = []
                for ev in rows:
                    if ev["side"] not in ("IN", "OUT"):
                        continue
                    if ev["uniq"] in seen:
                        continue
                    seen.add(ev["uniq"])
                    max_blk = max(max_blk, ev["block"])
                    px = _get_price(ev["symbol"], ev.get("contract"))
                    ev["price_usd"] = px
                    _append_csv(ev["ts"], ev["symbol"], ev["qty"], ev["side"], px, ev["tx"])
                    evs.append(ev)

                if evs:
                    txt = "🟡 Live Trades\n" + _fmt_alert(evs)
                    await _safe_send(send_fn, txt)

                last_blk = max_blk if rows else last_blk

            except Exception as e:
                logger.exception(f"monitor loop error: {e}")
            await asyncio.sleep(MONITOR_POLL)

# -----------------------------
# Safe sender
# -----------------------------
async def _safe_send(send_fn, text):
    try:
        res = send_fn(text)
        if asyncio.iscoroutine(res):
            await res
    except Exception:
        logging.getLogger("realtime").exception("send failed")
