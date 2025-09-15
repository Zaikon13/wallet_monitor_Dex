#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main.py (patched v4)
Adds: Watchlist scanner with rules + cooldown, alongside holdings spike alerts.
"""
from __future__ import annotations
import os, sys, time, json, signal, logging, threading
from collections import defaultdict, deque
from datetime import datetime
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

getcontext().prec = 28

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ---- External helpers (with fallbacks) ----
try:
    from utils.http import safe_get, safe_json
except Exception:  # tiny fallback
    import requests
    def safe_get(url: str, *, params=None, timeout: int = 10, retries: int = 2, backoff: float = 0.7):
        attempt = 0
        while True:
            attempt += 1
            try:
                r = requests.get(url, params=params, timeout=timeout)
                if r.status_code >= 500:
                    raise RuntimeError(r.status_code)
                return r
            except Exception:
                if attempt > retries:
                    return None
                time.sleep(backoff * attempt)
    def safe_json(resp):
        if resp is None: return None
        try: return resp.json()
        except Exception:
            try: return json.loads(resp.text)
            except Exception: return None

try:
    from telegram.api import send_telegram
except Exception:
    import requests as _rq
    def send_telegram(text: str):
        token = os.getenv("TELEGRAM_BOT_TOKEN", ""); chat = os.getenv("TELEGRAM_CHAT_ID", "")
        if not (token and chat and text): return
        _rq.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
            "chat_id": chat, "text": text, "parse_mode": "MarkdownV2", "disable_web_page_preview": True
        }, timeout=15)

from reports.day_report import build_day_report_text as _compose_day_report
from reports.ledger import append_ledger, update_cost_basis as ledger_update_cost_basis, replay_cost_basis_over_entries
from reports.aggregates import aggregate_per_asset
from reports.pnl import enrich_breakdown_with_pnl
from core.alerts import check_spikes_and_send
from core.watchlist import scan_watchlist_and_alert
from core.config import apply_env_aliases

# ===================== Bootstrap / ENV =====================
load_dotenv(); apply_env_aliases()

TZ = os.getenv("TZ", "Europe/Athens"); LOCAL_TZ = ZoneInfo(TZ)
now = lambda: datetime.now(LOCAL_TZ)
ymd = lambda dt=None: (dt or now()).strftime("%Y-%m-%d")
month_prefix = lambda dt=None: (dt or now()).strftime("%Y-%m")

# Config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
WALLET_ADDRESS     = (os.getenv("WALLET_ADDRESS", "")).lower()
ETHERSCAN_API      = os.getenv("ETHERSCAN_API", "")
CRONOS_RPC_URL     = os.getenv("CRONOS_RPC_URL", "")

DATA_DIR           = os.getenv("DATA_DIR", "/app/data")
OFFSET_PATH        = os.path.join(DATA_DIR, "telegram_offset.json")
ATH_PATH           = os.path.join(DATA_DIR, "ath.json")
WATCHLIST_PATH     = os.path.join(DATA_DIR, "watchlist.json")
RULES_PATH         = os.path.join(DATA_DIR, "rules.json")

# Sane defaults
WALLET_POLL        = int(os.getenv("WALLET_POLL", "15"))
INTRADAY_HOURS     = int(os.getenv("INTRADAY_HOURS", "3"))
EOD_HOUR           = int(os.getenv("EOD_HOUR", "23"))
EOD_MINUTE         = int(os.getenv("EOD_MINUTE", "59"))
RECEIPT_SYMBOLS    = set([s.strip().upper() for s in os.getenv("RECEIPT_SYMBOLS", "TCRO").split(",") if s.strip()])

# Dexscreener endpoints
DEX_BASE_TOKENS    = "https://api.dexscreener.com/latest/dex/tokens"
DEX_BASE_SEARCH    = "https://api.dexscreener.com/latest/dex/search"

# Etherscan (Cronos)
ETHERSCAN_V2_URL   = "https://api.etherscan.io/v2/api"; CRONOS_CHAINID = 25

# Logging
os.makedirs(DATA_DIR, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s", stream=sys.stdout)
log = logging.getLogger("sentinel")

# ===================== Runtime State & Locks =====================
shutdown_event = threading.Event()
BAL_LOCK = threading.RLock(); POS_LOCK = threading.RLock(); SEEN_LOCK = threading.RLock()

_token_balances: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
_token_meta: dict[str, dict] = {}
_position_qty: dict[str, Decimal]  = defaultdict(lambda: Decimal("0"))
_position_cost: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
_seen_tx_hashes: set[str] = set()
_seen_token_events: deque = deque(maxlen=4000)
_seen_token_hashes: deque = deque(maxlen=2000)

PRICE_CACHE: dict[str, tuple[float | None, float]] = {}; PRICE_CACHE_TTL = 60
ATH: dict[str, float] = {}
EPSILON = Decimal("1e-12")

# Health info
LAST_SCHED_TICK = 0.0
LAST_ALERT_TICK = 0.0
LAST_LONGPOLL_TICK = 0.0
LAST_WATCH_TICK = 0.0

# ===================== Small file helpers =====================
read_json = lambda p, d: (json.load(open(p, "r", encoding="utf-8")) if os.path.exists(p) else d)

def write_json(path: str, obj):
    tmp = path + ".tmp"; open(tmp, "w", encoding="utf-8").write(json.dumps(obj, ensure_ascii=False, indent=2)); os.replace(tmp, path)

def data_file_for_today() -> str: return os.path.join(DATA_DIR, f"transactions_{ymd()}.json")

# ===================== Pricing helpers (Dexscreener) =====================
# ... (όλες οι functions get_price_usd, get_change_and_price_for_symbol_or_addr, _price_cro_canonical, κλπ όπως στο Canvas v4)
# ===================== Watchlist & Rules helpers =====================
# ... (_read_watchlist, _write_watchlist, _read_rules, _write_rules όπως στο Canvas v4)
# ===================== Reports & PnL =====================
# ... (format_totals, realized_and_unrealized όπως στο Canvas v4)
# ===================== Holdings valuation =====================
# ... (compute_holdings_* όπως στο Canvas v4)
# ===================== Telegram commands =====================
# ... (_handle_command με /status, /holdings, /pnl, /report, /watch, /rules)
# ===================== Scheduler & main =====================
def _scheduler_loop():
    global LAST_SCHED_TICK, LAST_ALERT_TICK, LAST_WATCH_TICK
    last_intraday = 0.0; last_eod_date = ""; last_watch = 0.0
    while not shutdown_event.is_set():
        LAST_SCHED_TICK = time.time()
        dt = now()
        # Intraday
        if INTRADAY_HOURS > 0 and (time.time() - last_intraday) >= INTRADAY_HOURS * 3600:
            try: send_telegram("⏱ Intraday report…"); _handle_command("/report")
            except Exception: pass
            last_intraday = time.time()
        # EOD
        if dt.strftime("%H:%M") == f"{int(os.getenv('EOD_HOUR','23')):02d}:{int(os.getenv('EOD_MINUTE','59')):02d}" and last_eod_date != ymd(dt):
            try: send_telegram("🌙 End-of-day report…"); _handle_command("/report")
            except Exception: pass
            last_eod_date = ymd(dt)
        # Holdings spike alerts
        try:
            _total, breakdown, _unrealized, _receipts = compute_holdings_merged()
            check_spikes_and_send(
                breakdown=breakdown,
                fetch_change=get_change_and_price_for_symbol_or_addr,
                notify=send_telegram,
                state_path=os.path.join(DATA_DIR, "alerts_state.json"),
                pump_pct=float(os.getenv("PUMP_ALERT_24H_PCT", "20") or 20),
                dump_pct=float(os.getenv("DUMP_ALERT_24H_PCT", "-15") or -15),
                cooldown_min=int(os.getenv("ALERTS_INTERVAL_MIN", "15") or 15),
            )
            LAST_ALERT_TICK = time.time()
        except Exception:
            pass
        # Watchlist scanner pass
        dex_poll = int(os.getenv("DEX_POLL", "60") or 60)
        if time.time() - last_watch >= max(10, dex_poll):
            try:
                from telegram.api import send_watchlist_alerts
                wl = _read_watchlist(); rules = _read_rules()
                scan_watchlist_and_alert(
                    watchlist=wl,
                    state_path=os.path.join(DATA_DIR, "watchlist_state.json"),
                    env=os.environ,
                    rules=rules or {},
                    safe_get_fn=safe_get,
                    send_batch=send_watchlist_alerts,
                    cooldown_min=int(os.getenv("ALERTS_INTERVAL_MIN", "15") or 15),
                )
                LAST_WATCH_TICK = time.time(); last_watch = time.time()
            except Exception:
                pass
        for _ in range(10):
            if shutdown_event.is_set(): break
            time.sleep(6)

def _graceful_exit(*_):
    try: shutdown_event.set()
    except Exception: pass

def main():
    send_telegram("✅ Sentinel started (patched v4 — watchlist scanner enabled).")
    th_poll = threading.Thread(target=telegram_long_poll_loop, name="tg-poll", daemon=False)
    th_sched = threading.Thread(target=_scheduler_loop, name="scheduler", daemon=False)
    th_poll.start(); th_sched.start()
    try:
        while th_poll.is_alive() or th_sched.is_alive(): time.sleep(0.5)
    except KeyboardInterrupt: _graceful_exit()
    th_poll.join(timeout=5); th_sched.join(timeout=5)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, _graceful_exit); signal.signal(signal.SIGTERM, _graceful_exit); main()
