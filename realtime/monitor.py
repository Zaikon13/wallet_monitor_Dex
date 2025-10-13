# realtime/monitor.py
# Live wallet monitor για Cronos: auto-discover όλων των CRC-20 transfers (χωρίς allowlist),
# Telegram trade alerts + append σε data/ledger.csv, με ελαφρύ polling μέσω explorer API.
from __future__ import annotations

import os
import io
import csv
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Set
from datetime import datetime
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, getcontext

import aiohttp

# Προαιρετικό: χρησιμοποιούμε μόνο για να πάρουμε current block στο boot.
# Αν λείπει ή αποτύχει, συνεχίζουμε κανονικά με explorer.
try:
    from web3 import Web3
except Exception:  # pragma: no cover
    Web3 = None  # type: ignore

# Τιμολόγηση (best-effort)
try:
    from core.pricing import get_spot_usd
except Exception:
    def get_spot_usd(symbol: str, token_address: Optional[str] = None) -> Optional[Decimal]:
        return None  # fallback: χωρίς τιμή

# -----------------------------
# ENV / Defaults
# -----------------------------
TZ = os.getenv("TZ", "Europe/Athens")
LEDGER_CSV = os.getenv("LEDGER_CSV", "data/ledger.csv")
WALLET = (os.getenv("WALLET_ADDRESS") or "").strip()
if WALLET:
    WALLET_LC = WALLET.lower()
else:
    WALLET_LC = ""

MONITOR_MODE = (os.getenv("MONITOR_MODE", "") or "").lower() or "explorer"
POLL_SEC = float(os.getenv("MONITOR_POLL_SECONDS", os.getenv("RT_POLL_SEC", "4")))

# Explorer (BlockScout/Etherscan-style)
EXPLORER_BASE = (os.getenv("CRONOS_EXPLORER_API_BASE", "https://cronos.org/explorer/api").rstrip("/"))
EXPLORER_KEY = os.getenv("CRONOS_EXPLORER_API_KEY", "").strip()

# Προαιρετικό RPC για seed του current block:
RPC_URL = os.getenv("CRONOS_RPC_URL", "").split(",")[0].strip()

# Αριθμητικά
getcontext().prec = 36

# -----------------------------
# CSV helpers
# -----------------------------
CSV_FIELDS = ["ts", "symbol", "qty", "side", "price_usd", "tx"]

def _ensure_dirs():
    base = os.path.dirname(LEDGER_CSV)
    if base:
        os.makedirs(base, exist_ok=True)

def _append_to_ledger_csv(ts_iso: str, symbol: str, qty: Decimal | str,
                          side: str, price_usd: Decimal | str, tx_hash: str) -> None:
    _ensure_dirs()
    exists = os.path.isfile(LEDGER_CSV)
    try:
        with open(LEDGER_CSV, "a", newline="", encoding="utf-8") as fp:
            w = csv.DictWriter(fp, fieldnames=CSV_FIELDS)
            if not exists:
                w.writeheader()
            w.writerow({
                "ts": ts_iso,
                "symbol": symbol.upper(),
                "qty": str(qty),
                "side": side.upper(),
                "price_usd": str(price_usd),
                "tx": tx_hash,
            })
    except Exception as e:
        logging.getLogger("realtime").exception(f"Failed writing {LEDGER_CSV}: {e}")

# -----------------------------
# Explorer client
# -----------------------------
async def _explorer_tokentx(
    session: aiohttp.ClientSession,
    wallet: str,
    startblock: Optional[int] = None,
    sort: str = "asc",
    logger: Optional[logging.Logger] = None,
) -> List[Dict[str, Any]]:
    """
    BlockScout/Etherscan συμβατό endpoint:
    ?module=account&action=tokentx&address=<wallet>&startblock=<n>&sort=asc
    Επιστρέφει λίστα από token transfers που αφορούν το wallet (in/out).
    """
    params = {
        "module": "account",
        "action": "tokentx",
        "address": wallet,
        "sort": sort,
    }
    if startblock is not None:
        params["startblock"] = str(startblock + 1)
    if EXPLORER_KEY:
        params["apikey"] = EXPLORER_KEY

    url = f"{EXPLORER_BASE}/"
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            data = await r.json()
    except Exception as e:
        logger and logger.warning(f"explorer request failed: {e}")
        return []

    result = data.get("result")
    if not result or isinstance(result, str):
        return []
    if not isinstance(result, list):
        return []
    return result

def _to_dec(x: Any) -> Decimal:
    try:
        return Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")

def _parse_tokentx_row(row: Dict[str, Any], tz: str) -> Dict[str, Any]:
    """
    Μετατρέπει explorer row -> unified event.
    """
    # Required fields with fallbacks
    blk = int(row.get("blockNumber") or 0)
    ts_unix = int(row.get("timeStamp") or 0)
    txh = str(row.get("hash") or "")
    frm = str(row.get("from") or "").lower()
    to = str(row.get("to") or "").lower()
    decimals = int(row.get("tokenDecimal") or 18)
    symbol = (row.get("tokenSymbol") or "").upper() or "TOKEN"
    contract = row.get("contractAddress") or ""

    qty_raw = _to_dec(row.get("value") or "0")
    base = Decimal(10) ** decimals
    qty = qty_raw / base if base > 0 else qty_raw

    direction = "NA"
    wl = WALLET_LC
    if wl and to == wl:
        direction = "IN"
    elif wl and frm == wl:
        direction = "OUT"

    ts = datetime.fromtimestamp(ts_unix, tz=ZoneInfo(tz)).isoformat(timespec="seconds")

    # unique id for dedup
    uniq = f"{txh}:{row.get('logIndex', '')}:{symbol}:{direction}:{qty}"

    return dict(
        block=blk, ts_iso=ts, tx=txh, side=direction, symbol=symbol,
        qty=qty, contract=contract, uniq=uniq
    )

async def _seed_latest_block(logger: Optional[logging.Logger]) -> Optional[int]:
    """
    Παίρνει τον current block για να μην ξαναστείλουμε παλιές μεταφορές στην εκκίνηση.
    Προαιρετικό (αν αποτύχει, συνεχίζουμε με None).
    """
    if not RPC_URL or not Web3:
        return None
    try:
        w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 10}))
        blk = int(w3.eth.block_number)
        logger and logger.info(f"Realtime: seed latest block via RPC = {blk}")
        return blk
    except Exception as e:
        logger and logger.warning(f"seed latest block failed: {e}")
        return None

# -----------------------------
# Pricing
# -----------------------------
def _get_price(symbol: str, contract: Optional[str]) -> Decimal:
    try:
        px = get_spot_usd(symbol, token_address=contract)
        if px is None:
            return Decimal("0")
        return _to_dec(px)
    except Exception:
        return Decimal("0")

# -----------------------------
# Alert formatting
# -----------------------------
def _fmt_qty(x: Decimal) -> str:
    # 6 δεκαδικά για token qty
    try:
        return f"{x:,.6f}"
    except Exception:
        return str(x)

def _fmt_price(x: Decimal) -> str:
    try:
        if x >= Decimal("1000"):
            return f"{x:,.2f}"
        if x >= Decimal("1"):
            return f"{x:.2f}"
        elif x >= Decimal("0.01"):
            return f"{x:.6f}"
        else:
            s = f"{x:.8f}".rstrip("0").rstrip(".")
            return s or "0"
    except Exception:
        return str(x)

def _format_alert_lines(events: List[Dict[str, Any]]) -> str:
    lines = []
    for ev in events:
        side_emoji = "🟢 IN" if ev["side"] == "IN" else ("🔴 OUT" if ev["side"] == "OUT" else "•")
        px = ev.get("price_usd") or Decimal("0")
        lines.append(
            f"• {ev['ts_iso'][-8:]} — {side_emoji} {ev['symbol']} {_fmt_qty(ev['qty'])} @ ${_fmt_price(px)}"
        )
    return "\n".join(lines)

# -----------------------------
# MAIN LOOP
# -----------------------------
async def monitor_wallet(send_fn, logger: Optional[logging.Logger] = None):
    """
    Κύριος βρόχος. Σε explorer mode:
      - τραβάει ΟΛΑ τα CRC-20 transfers του wallet
      - melakukan dedup
      - στέλνει Telegram alerts
      - γράφει στο ledger.csv
    """
    if not WALLET:
        raise RuntimeError("WALLET_ADDRESS is not set")

    logger = logger or logging.getLogger("realtime")
    logger.setLevel(logging.INFO)

    # Καθορισμός λειτουργίας:
    # Αν ΜΗΔΕΝΙΚΟ allowlist => επιλέγουμε explorer mode by default
    allowlist = os.getenv("MONITOR_TOKEN_ADDRESSES", "").strip()
    mode = MONITOR_MODE
    if not allowlist:
        mode = "explorer"

    logger.info(f"Realtime monitor mode: {mode} (poll={POLL_SEC}s)")

    # Εκκίνηση: κρατάμε "τελευταίο block" για να μην ξαναστείλουμε παλιά συμβάντα
    last_seen_block: Optional[int] = await _seed_latest_block(logger)

    # Dedup των event ids σε μνήμη
    seen: Set[str] = set()

    # client
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                # ------------------ Explorer mode ------------------
                # Αν έχεις απλά βάλει MONITOR_MODE=explorer ή δεν έχεις allowlist, ερχόμαστε εδώ.
                if mode == "explorer":
                    rows = await _explorer_tokentx(session, WALLET, startblock=last_seen_block, logger=logger)
                    if rows:
                        # Μετατροπή & dedup
                        events: List[Dict[str, Any]] = []
                        max_blk = last_seen_block or 0
                        for r in rows:
                            ev = _parse_tokentx_row(r, TZ)
                            if not ev["side"] in ("IN", "OUT"):
                                continue
                            if ev["uniq"] in seen:
                                continue
                            seen.add(ev["uniq"])
                            max_blk = max(max_blk, int(ev["block"]))
                            # pricing best-effort
                            px = _get_price(ev["symbol"], ev.get("contract"))
                            ev["price_usd"] = px
                            # write CSV
                            _append_to_ledger_csv(ev["ts_iso"], ev["symbol"], ev["qty"], ev["side"], px, ev["tx"])
                            events.append(ev)

                        if events:
                            txt = "🟡 Intraday Trades\n" + _format_alert_lines(events)
                            await _safe_send(send_fn, txt)
                        last_seen_block = max_blk if rows else last_seen_block

                # ------------------ RPC allowlist mode (προαιρετικά) ------------------
                else:
                    # Αν για κάποιο λόγο θες να κρατήσεις το παλιό allowlist-RPC pipeline,
                    # μπορείς να τοποθετήσεις εδώ τον υπάρχοντα κώδικα σου (_rpc_monitor_mode).
                    # Για την ώρα απλά κάνουμε fallback σε explorer ώστε να μη μείνεις χωρίς alerts.
                    rows = await _explorer_tokentx(session, WALLET, startblock=last_seen_block, logger=logger)
                    if rows:
                        events: List[Dict[str, Any]] = []
                        max_blk = last_seen_block or 0
                        for r in rows:
                            ev = _parse_tokentx_row(r, TZ)
                            if not ev["side"] in ("IN", "OUT"):
                                continue
                            if ev["uniq"] in seen:
                                continue
                            seen.add(ev["uniq"])
                            max_blk = max(max_blk, int(ev["block"]))
                            px = _get_price(ev["symbol"], ev.get("contract"))
                            ev["price_usd"] = px
                            _append_to_ledger_csv(ev["ts_iso"], ev["symbol"], ev["qty"], ev["side"], px, ev["tx"])
                            events.append(ev)

                        if events:
                            txt = "🟡 Intraday Trades (fallback)\n" + _format_alert_lines(events)
                            await _safe_send(send_fn, txt)
                        last_seen_block = max_blk if rows else last_seen_block

            except Exception as e:
                logger.exception(f"monitor loop error: {e}")

            await asyncio.sleep(POLL_SEC)

# -----------------------------
# Safe sender
# -----------------------------
async def _safe_send(send_fn, text: str):
    try:
        maybe = send_fn(text)
        if asyncio.iscoroutine(maybe):
            await maybe
    except Exception:
        logging.getLogger("realtime").exception("Failed to send alert")
