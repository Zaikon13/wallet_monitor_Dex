from __future__ import annotations

"""
Cronos DeFi Sentinel — main.py (FULL, 3 parts)

Features:
- RPC snapshot (CRO + ERC-20)
- Dexscreener pricing (+history fallback)
- Cost-basis PnL (realized & unrealized)
- Intraday/EOD reports
- Alerts (24h pump/dump) & Guard window
- Telegram long-poll commands:
  /status, /diag, /rescan
  /holdings, /show_wallet_assets, /showwalletassets, /show
  /dailysum, /showdaily, /report
  /totals, /totalstoday, /totalsmonth
  /pnl [today|month|all]

Compatible helpers:
  utils/http.py, telegram/api.py, reports/day_report.py,
  reports/ledger.py, reports/aggregates.py
"""

# =====================================
# PART I — Bootstrap & Imports
# =====================================
import argparse
import logging
import os
import sys
import threading
import time
from typing import Callable, List, Optional

# Optional .env
try:  # pragma: no cover
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    def load_dotenv(*_a, **_k):
        return False

load_dotenv()

# Core config/state
from core.config import apply_env_aliases, validate_env
from core.runtime_state import note_tick, update_snapshot
from core.holdings import get_wallet_snapshot
from core.wallet_monitor import make_wallet_monitor
from core.providers.cronos import fetch_wallet_txs
from core.signals.server import start_signals_server_if_enabled

# Reports & PnL
from reports.day_report import build_day_report_text
from reports.ledger import update_cost_basis

# Telegram I/O
from telegram.api import send_telegram, telegram_long_poll_loop


# =====================================
# PART II — Services, Dispatcher & Runners
# =====================================

def _bootstrap_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def _bootstrap_env(strict: bool = False) -> None:
    apply_env_aliases()
    ok, warns = validate_env(strict=strict)
    for w in warns:
        logging.warning(w)
    if not ok:
        logging.warning("Environment not fully configured; some features may be disabled.")


# --- Command aliases over telegram.commands ---
from telegram import commands as tcmd


def _alias(text: str) -> List[str]:
    """Return replies for supported command aliases."""
    txt = (text or "").strip()
    if not txt:
        return []
    parts = txt.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd in {"/status"}:
        return [tcmd.handle_status()]
    if cmd in {"/diag"}:
        return [tcmd.handle_diag()]
    if cmd in {"/rescan"}:
        snap = get_wallet_snapshot()
        update_snapshot(snap, time.time())
        try:
            update_cost_basis()
        except Exception as exc:
            logging.debug("cost basis update failed: %s", exc)
        return ["Rescan completed."]

    if cmd in {"/holdings", "/show_wallet_assets", "/showwalletassets", "/show"}:
        return [tcmd.handle_holdings()]

    if cmd in {"/dailysum", "/showdaily", "/report"}:
        return [tcmd.handle_daily()]

    if cmd in {"/totals", "/totalstoday", "/totalsmonth"}:
        return [tcmd.handle_totals()]

    if cmd == "/pnl":
        scope = args[0].lower() if args else None
        if scope in {"today", "month", "all"}:
            return [tcmd.handle_pnl(scope)]
        return [tcmd.handle_pnl(args[0]) if args else tcmd.handle_pnl(None)]

    return []


def _tg_loop_worker() -> None:
    telegram_long_poll_loop(_alias)


def _start_thread(target: Callable[[], None], name: str) -> threading.Thread:
    t = threading.Thread(target=target, name=name, daemon=True)
    t.start()
    return t


def _single_poll_and_report() -> int:
    mon = make_wallet_monitor(provider=fetch_wallet_txs)
    polled = mon.poll_once()
    snap = get_wallet_snapshot()
    update_snapshot(snap, time.time())
    text = build_day_report_text(
        intraday=False, wallet=os.getenv("WALLET_ADDRESS", ""), snapshot=snap
    )
    print(text)
    return 0 if polled >= 0 else 1


def _diag_print() -> int:
    print("Cronos DeFi Sentinel — quick diagnostics…")
    _bootstrap_env(False)
    print("Env validation completed. If warnings above, check .env.")
    return 0


# =====================================
# PART III — CLI Entrypoint
# =====================================

def main(argv: Optional[List[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    parser = argparse.ArgumentParser(prog="Cronos DeFi Sentinel")
    parser.add_argument("--once", action="store_true", help="Single poll + snapshot + print daily")
    parser.add_argument("--diag", action="store_true", help="Quick diagnostics and exit")
    args = parser.parse_args(argv)

    _bootstrap_logging()
    _bootstrap_env(False)

    if args.diag:
        return _diag_print()
    if args.once:
        return _single_poll_and_report()

    # Long-running mode
    start_signals_server_if_enabled()  # HTTP /healthz + /signal (guard window)
    _start_thread(_tg_loop_worker, "tg-long-poll")

    mon = make_wallet_monitor(provider=fetch_wallet_txs)
    poll_s = max(5, int(os.getenv("WALLET_POLL_INTERVAL", "30") or 30))
    logging.info("Poll interval: %ss", poll_s)

    while True:
        try:
            note_tick()
            mon.poll_once()
            snap = get_wallet_snapshot()
            update_snapshot(snap, time.time())
        except Exception as exc:  # pragma: no cover
            logging.debug("main loop error: %s", exc, exc_info=True)
            try:
                send_telegram(f"⚠️ runtime error: {exc}")
            except Exception:
                pass
        finally:
            time.sleep(poll_s)


if __name__ == "__main__":
    raise SystemExit(main())
