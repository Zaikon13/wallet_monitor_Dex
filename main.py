#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, time, logging, signal
from datetime import datetime
from decimal import Decimal
from typing import Optional, Dict, Any, List
import requests
from dotenv import load_dotenv
import schedule

# ------------ Logging ------------
def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

# ------------ Env helpers ------------
def env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None: return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")

def get_eod_time() -> str:
    t = os.getenv("EOD_TIME", "23:59").strip()
    try:
        hh, mm = t.split(":"); ih, im = int(hh), int(mm)
        if 0 <= ih <= 23 and 0 <= im <= 59:
            return f"{ih:02d}:{im:02d}"
    except Exception:
        pass
    logging.warning("Invalid EOD_TIME '%s' → using 23:59", t)
    return "23:59"

def get_intraday_hours() -> Optional[int]:
    v = os.getenv("INTRADAY_HOURS", "").strip()
    if not v: return None
    try:
        iv = int(v)
        return iv if iv > 0 else None
    except Exception:
        logging.warning("Invalid INTRADAY_HOURS '%s' (ignored)", v)
        return None

# ------------ Telegram (plain text) ------------
def send_telegram(text: str) -> None:
    bot = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        logging.info("Telegram not configured (TELEGRAM_BOT_TOKEN/CHAT_ID missing)")
        return
    url = f"https://api.telegram.org/bot{bot}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
            timeout=12,
        )
        if r.status_code != 200:
            logging.warning("Telegram send failed %s: %s", r.status_code, r.text)
    except Exception as e:
        logging.exception("Telegram send error: %s", e)

# ------------ JSON-RPC (Cronos) ------------
def _rpc_call(method: str, params: List[Any]) -> Any:
    rpc = os.getenv("CRONOS_RPC_URL", "").strip()
    if not rpc:
        logging.warning("CRONOS_RPC_URL missing")
        return None
    try:
        resp = requests.post(rpc, json={"jsonrpc":"2.0","id":1,"method":method,"params":params}, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            logging.warning("RPC error: %s", data["error"])
            return None
        return data.get("result")
    except Exception as e:
        logging.exception("RPC %s failed: %s", method, e)
        return None

def _hex_to_int(h: str) -> int:
    try: return int(h, 16)
    except Exception: return 0

def rpc_get_cro_balance(address: str) -> Decimal:
    res = _rpc_call("eth_getBalance", [address, "latest"])
    if not isinstance(res, str): return Decimal("0")
    wei = _hex_to_int(res)
    return Decimal(wei) / (Decimal(10) ** 18)

def rpc_get_erc20_balance(contract: str, address: str) -> Decimal:
    # balanceOf(address) selector 0x70a08231 + 32-byte padded address
    addr = address.lower()
    if addr.startswith("0x"): addr = addr[2:]
    data = "0x70a08231" + "0"*24 + addr
    call = {"to": contract, "data": data}
    res = _rpc_call("eth_call", [call, "latest"])
    if not isinstance(res, str): return Decimal("0")
    val = _hex_to_int(res)
    return Decimal(val)  # decimals applied later

def _map_from_env(key: str) -> dict:
    s = os.getenv(key, "").strip()
    if not s: return {}
    out = {}
    for part in s.split(","):
        if not part or "=" not in part: continue
        k, v = part.split("=", 1)
        out[k.strip().upper()] = v.strip()
    return out

# ------------ Holdings (self-contained) ------------
def _D(x: Any) -> Decimal:
    try: return Decimal(str(x))
    except Exception: return Decimal("0")

def build_snapshot_rescue(address: str) -> Dict[str, Dict[str, Any]]:
    snap: Dict[str, Dict[str, Any]] = {}

    # CRO native
    try:
        cro = rpc_get_cro_balance(address)
        snap["CRO"] = {"amount": cro, "price": Decimal("0")}
    except Exception as e:
        logging.warning("CRO balance read failed: %s", e)

    # ERC-20 from env maps (optional)
    addrs = _map_from_env("TOKENS_ADDRS")
    decs  = _map_from_env("TOKENS_DECIMALS")
    for sym, contract in (addrs or {}).items():
        try:
            raw = rpc_get_erc20_balance(contract, address)
            dec = int(decs.get(sym, "18"))
            qty = raw / (Decimal(10) ** dec)
            snap[sym] = {"amount": qty, "price": Decimal("0")}
        except Exception as e:
            logging.warning("ERC20 %s read failed: %s", sym, e)
            if sym not in snap:
                snap[sym] = {"amount": Decimal("0"), "price": Decimal("0")}
    return snap

def _format_holdings_plain(snapshot: Dict[str, Dict[str, Any]]) -> str:
    if not snapshot: return "Empty snapshot."
    lines = ["Holdings Snapshot", ""]
    total = Decimal("0")
    for sym, item in snapshot.items():
        qty = _D(item.get("amount", item.get("qty", 0)))
        price = _D(item.get("price", item.get("price_usd", 0)))
        usd = qty * price
        total += usd
        lines.append(f"{sym:<8} {qty:>12,.4f} x ${price:,.6f} = ${usd:,.2f}")
    lines.append("")
    lines.append(f"Total: ${total:,.2f}")
    return "\n".join(lines)

def job_holdings_snapshot() -> None:
    wallet = os.getenv("WALLET_ADDRESS", "").strip()
    if not wallet:
        send_telegram("🧾 Holdings: WALLET_ADDRESS missing.")
        return
    try:
        snap = build_snapshot_rescue(wallet)
        if not snap:
            send_telegram("🧾 Holdings: Empty snapshot.")
            return
        send_telegram("🧾 " + _format_holdings_plain(snap))
    except Exception as e:
        logging.exception("job_holdings_snapshot failed: %s", e)
        try: send_telegram("⚠️ Holdings snapshot failed.")
        except Exception: pass

# ------------ Reports ------------
from reports.day_report import build_day_report_text

def job_startup() -> None:
    send_telegram("✅ Sentinel started (rescue+rpc-holdings).")

def job_daily_report() -> None:
    try:
        text = build_day_report_text()
    except Exception:
        today = datetime.now().strftime("%Y-%m-%d")
        text = f"📒 Daily Report — {today}\nStatus: OK"
    send_telegram(text)

# ------------ Loop ------------
def bind_schedules() -> None:
    schedule.every().day.at(get_eod_time()).do(job_daily_report)
    logging.info("Scheduled daily report at %s", get_eod_time())
    ih = get_intraday_hours()
    if ih:
        schedule.every(ih).hours.do(job_holdings_snapshot)
        logging.info("Scheduled holdings snapshot every %s hour(s)", ih)

def run_loop(stop: list[bool]) -> None:
    while not stop[0]:
        try:
            schedule.run_pending()
        except Exception as e:
            logging.exception("schedule.run_pending failed: %s", e)
        time.sleep(1)

def install_signals(stop: list[bool]) -> None:
    def _grace(sig, frame):
        logging.info("Signal %s — stopping...", sig)
        stop[0] = True
        try: send_telegram("🛑 Sentinel stopping...")
        except Exception: pass
    try:
        signal.signal(signal.SIGINT, _grace)
        signal.signal(signal.SIGTERM, _grace)
    except Exception:
        pass

def main() -> int:
    setup_logging()
    try: load_dotenv(override=False)
    except Exception: pass

    logging.info("Starting Sentinel (rescue+rpc-holdings)...")
    job_startup()

    if env_bool("STARTUP_SNAPSHOT", True):
        job_holdings_snapshot()

    bind_schedules()
    stop = [False]
    install_signals(stop)
    try:
        run_loop(stop)
    except Exception as e:
        logging.exception("Fatal in main loop: %s", e)
        try: send_telegram("❌ Fatal error in main loop.")
        except Exception: pass
        return 1
    logging.info("Exit.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
