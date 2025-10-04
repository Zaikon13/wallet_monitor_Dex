#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cronos DeFi Sentinel — main entrypoint

- No network/disk I/O on import
- Loads config at runtime
- Starts scheduler jobs (daily/weekly reports)
- Starts Telegram dispatcher (if tokens present)
- Graceful shutdown on SIGINT/SIGTERM
- Safe fallbacks if individual modules/APIs slightly differ across snapshots

Run:
    python3 main.py

Railway Procfile:
    web: python3 main.py
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from types import ModuleType
from typing import Any, Callable, Optional

# Third-party (declared in requirements.txt)
# schedule is imported lazily in App.start() to avoid import side-effects globally

# --- Optional imports (guarded) ------------------------------------------------
def _try_import(name: str) -> Optional[ModuleType]:
    try:
        __import__(name)
        return sys.modules[name]
    except Exception:
        return None


# Config
_cfg_mod = _try_import("core.config")
if _cfg_mod:
    AppConfig = getattr(_cfg_mod, "AppConfig", None)
    load_config = getattr(_cfg_mod, "load_config", None)
else:
    AppConfig = None
    load_config = None

# Timezone
_tz_mod = _try_import("core.tz")
local_tz = getattr(_tz_mod, "local_tz", lambda: None) if _tz_mod else (lambda: None)

# Telegram
_tg_api_mod = _try_import("telegram.api")
_tg_fmt_mod = _try_import("telegram.formatters")
_tg_disp_mod = _try_import("telegram.dispatcher")

escape_md_v2: Callable[[str], str]
format_holdings: Callable[[dict[str, Any]], str]

if _tg_fmt_mod:
    escape_md_v2 = getattr(_tg_fmt_mod, "escape_md_v2", lambda s: s)
    format_holdings = getattr(_tg_fmt_mod, "format_holdings", lambda snap: str(snap))
else:
    escape_md_v2 = lambda s: s  # type: ignore
    format_holdings = lambda snap: str(snap)  # type: ignore

# Reports
_day_mod = _try_import("reports.day_report")
build_day_report_text = getattr(_day_mod, "build_day_report_text", None) if _day_mod else None

_weekly_mod = _try_import("reports.weekly")
build_weekly_report_text = getattr(_weekly_mod, "build_weekly_report_text", None) if _weekly_mod else None

# Holdings (optional)
_hold_mod = _try_import("core.holdings")
get_snapshot = getattr(_hold_mod, "get_snapshot", None) if _hold_mod else None

# Alerts (fallback to logging inside)
_alerts_mod = _try_import("core.alerts")
notify_error = getattr(_alerts_mod, "notify_error", None) if _alerts_mod else None
notify_warning = getattr(_alerts_mod, "notify_warning", None) if _alerts_mod else None


# --- Runtime state -------------------------------------------------------------
@dataclass
class RuntimeFlags:
    no_telegram: bool = False
    no_reports: bool = False
    no_dispatcher: bool = False


class App:
    def __init__(self) -> None:
        self.log = logging.getLogger("main")
        self.stop_event = threading.Event()
        self.flags = RuntimeFlags()
        self.cfg = self._load_cfg()
        self.tz = None

        # threads
        self._th_scheduler: Optional[threading.Thread] = None
        self._th_dispatcher: Optional[threading.Thread] = None

    # ---------- Setup ----------
    def _load_cfg(self) -> Any:
        """
        Load config using core.config if available, otherwise simple env shim.
        Must not perform any network I/O.
        """
        if load_config:
            try:
                return load_config()
            except Exception as e:
                self.log.exception("load_config() failed, falling back to env shim: %s", e)

        # Env shim
        class _Shim:
            TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
            TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
            WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
            CRONOS_RPC_URL = os.getenv("CRONOS_RPC_URL", "")
            ETHERSCAN_API = os.getenv("ETHERSCAN_API", "")
            TZ = os.getenv("TZ", "Europe/Athens")
            EOD_HOUR = int(os.getenv("EOD_HOUR", "23"))
            EOD_MINUTE = int(os.getenv("EOD_MINUTE", "55"))
            WEEKLY_DOW = int(os.getenv("WEEKLY_DOW", "6"))  # 0=Mon ... 6=Sun
            WEEKLY_HOUR = int(os.getenv("WEEKLY_HOUR", "21"))
            WEEKLY_MINUTE = int(os.getenv("WEEKLY_MINUTE", "0"))
            LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

        return _Shim()

    # ---------- Telegram ----------
    def _telegram_enabled(self) -> bool:
        if self.flags.no_telegram:
            return False
        tok = getattr(self.cfg, "TELEGRAM_BOT_TOKEN", "") or os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat = getattr(self.cfg, "TELEGRAM_CHAT_ID", "") or os.getenv("TELEGRAM_CHAT_ID", "")
        return bool(tok and chat)

    def _send_telegram(self, text: str) -> None:
        """
        Fire-and-forget send. If telegram layer is missing/disabled or call fails,
        logs instead of raising.
        """
        if not self._telegram_enabled():
            self.log.info("[TG disabled] %s", text)
            return

        text = escape_md_v2(str(text))

        # prefer telegram.api
        if _tg_api_mod:
            # try common function names
            for fn_name in ("send_message", "send_text", "send", "send_telegram", "send_telegram_message"):
                fn = getattr(_tg_api_mod, fn_name, None)
                if callable(fn):
                    try:
                        fn(text)  # type: ignore[arg-type]
                        return
                    except Exception as e:
                        self.log.warning("telegram.api.%s failed: %s", fn_name, e, exc_info=True)
                        break  # try next fallback
        # fallback to alerts
        if notify_warning:
            try:
                notify_warning(text)
                return
            except Exception:
                pass
        # final fallback — log
        self.log.info("[TG fallback->log] %s", text)

    # ---------- Schedulers ----------
    def _schedule_daily_report(self) -> None:
        if self.flags.no_reports:
            self.log.info("Daily report scheduling disabled by flags.")
            return

        import schedule  # local import to avoid global import side-effects

        hour = int(getattr(self.cfg, "EOD_HOUR", 23))
        minute = int(getattr(self.cfg, "EOD_MINUTE", 55))
        when = f"{hour:02d}:{minute:02d}"

        def job() -> None:
            try:
                text = self._build_day_report_safe()
                if text:
                    self._send_telegram(text)
            except Exception as e:
                self._handle_error("daily_report_job", e)

        schedule.every().day.at(when).do(job)
        self.log.info("Scheduled daily report at %s (local tz).", when)

    def _schedule_weekly_report(self) -> None:
        if self.flags.no_reports:
            self.log.info("Weekly report scheduling disabled by flags.")
            return

        import schedule

        dow = int(getattr(self.cfg, "WEEKLY_DOW", 6))  # 0 Mon ... 6 Sun
        hour = int(getattr(self.cfg, "WEEKLY_HOUR", 21))
        minute = int(getattr(self.cfg, "WEEKLY_MINUTE", 0))
        when = f"{hour:02d}:{minute:02d}"

        # pick schedule method based on DOW
        days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
        meth_name = days[dow] if 0 <= dow <= 6 else "sunday"
        meth = getattr(schedule.every(), meth_name)
        days_param = 7  # typical weekly span

        def job() -> None:
            try:
                text = self._build_weekly_report_safe(days=days_param)
                if text:
                    self._send_telegram(text)
            except Exception as e:
                self._handle_error("weekly_report_job", e)

        meth.at(when).do(job)
        self.log.info("Scheduled weekly report on %s at %s.", meth_name.capitalize(), when)

    def _build_day_report_safe(self) -> str:
        if callable(build_day_report_text):
            try:
                # Try with wallet param first, then without
                wallet = getattr(self.cfg, "WALLET_ADDRESS", "") or os.getenv("WALLET_ADDRESS", "")
                try:
                    return str(build_day_report_text(wallet=wallet))  # type: ignore[call-arg]
                except TypeError:
                    return str(build_day_report_text())  # type: ignore[misc]
            except Exception as e:
                self._handle_error("build_day_report_text", e)
        # fallback basic status
        return self._compose_status_text()

    def _build_weekly_report_safe(self, days: int = 7) -> str:
        if callable(build_weekly_report_text):
            try:
                wallet = getattr(self.cfg, "WALLET_ADDRESS", "") or os.getenv("WALLET_ADDRESS", "")
                try:
                    return str(build_weekly_report_text(days, wallet))  # type: ignore[misc]
                except TypeError:
                    try:
                        return str(build_weekly_report_text(days))  # type: ignore[misc]
                    except TypeError:
                        return str(build_weekly_report_text())  # type: ignore[misc]
            except Exception as e:
                self._handle_error("build_weekly_report_text", e)
        return f"Weekly report (fallback) — {datetime.now().strftime('%Y-%m-%d')}"

    # ---------- Status ----------
    def _compose_status_text(self) -> str:
        # Use holdings snapshot + formatters if available
        try:
            snap: Optional[dict[str, Any]] = None
            if callable(get_snapshot):
                snap = get_snapshot()  # type: ignore[call-arg]
            if snap and isinstance(snap, dict):
                return format_holdings(snap)
        except Exception as e:
            self._handle_error("compose_status:get_snapshot/format", e)

        # minimal fallback
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        return f"Status: OK — {now}"

    # ---------- Dispatcher ----------
    def _start_dispatcher(self) -> None:
        if self.flags.no_dispatcher:
            self.log.info("Telegram dispatcher disabled via flags.")
            return
        if not self._telegram_enabled():
            self.log.info("Telegram dispatcher not started (no tokens).")
            return
        if not _tg_disp_mod:
            self.log.info("Telegram dispatcher module missing.")
            return

        def runner() -> None:
            self.log.info("Telegram dispatcher starting...")
            # Try common API shapes
            for fn_name in ("run", "run_forever", "long_poll_loop", "main"):
                fn = getattr(_tg_disp_mod, fn_name, None)
                if callable(fn):
                    try:
                        fn(stop_event=self.stop_event)  # best-effort signature
                        return
                    except TypeError:
                        try:
                            fn()  # type: ignore[misc]
                            return
                        except Exception as e:
                            self._handle_error(f"dispatcher.{fn_name}", e)
                    except Exception as e:
                        self._handle_error(f"dispatcher.{fn_name}", e)
            self.log.info("Dispatcher exit (no suitable entry).")

        self._th_dispatcher = threading.Thread(target=runner, name="tg-dispatcher", daemon=True)
        self._th_dispatcher.start()

    # ---------- Top-level control ----------
    def start(self, *, no_telegram: bool = False, no_reports: bool = False, no_dispatcher: bool = False) -> None:
        # flags
        self.flags.no_telegram = no_telegram
        self.flags.no_reports = no_reports
        self.flags.no_dispatcher = no_dispatcher

        # logging
        level = (getattr(self.cfg, "LOG_LEVEL", "INFO") or "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, level, logging.INFO),
            format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        )
        self.log.info("Booting Cronos DeFi Sentinel...")

        # local tz (optional)
        try:
            self.tz = local_tz()
        except Exception:
            self.tz = None

        # startup message (optional)
        try:
            self._send_telegram("Starting Cronos DeFi Sentinel.")
        except Exception as e:
            self._handle_error("startup_telegram", e)

        # schedule jobs
        try:
            import schedule  # lazy import
            self._schedule_daily_report()
            self._schedule_weekly_report()

            # scheduler loop
            def scheduler_loop() -> None:
                self.log.info("Scheduler loop started.")
                while not self.stop_event.is_set():
                    try:
                        schedule.run_pending()
                    except Exception as e:
                        self._handle_error("schedule.run_pending", e)
                    time.sleep(1.0)
                self.log.info("Scheduler loop stopped.")

            self._th_scheduler = threading.Thread(target=scheduler_loop, name="scheduler", daemon=True)
            self._th_scheduler.start()
        except Exception as e:
            self._handle_error("scheduler_setup", e)

        # telegram dispatcher (long-poll)
        try:
            self._start_dispatcher()
        except Exception as e:
            self._handle_error("dispatcher_start", e)

        self.log.info("Startup complete.")

    def stop(self) -> None:
        if self.stop_event.is_set():
            return
        self.log.info("Shutting down...")
        self.stop_event.set()

        # join threads (best-effort)
        for th in (self._th_dispatcher, self._th_scheduler):
            if th and th.is_alive():
                th.join(timeout=5)

        # shutdown message
        try:
            self._send_telegram("Shutting down.")
        except Exception as e:
            self._handle_error("shutdown_telegram", e)

        self.log.info("Shutdown complete.")

    def _handle_error(self, where: str, err: BaseException) -> None:
        self.log.error("Error in %s: %s", where, err, exc_info=True)
        if notify_error:
            try:
                notify_error(f"{where}: {err}")
            except Exception:
                pass


# --- CLI ----------------------------------------------------------------------
def _parse_flags(argv: list[str]) -> RuntimeFlags:
    flags = RuntimeFlags()
    for a in argv[1:]:
        a = a.strip().lower()
        if a in ("--no-telegram", "--no_tg"):
            flags.no_telegram = True
        elif a in ("--no-reports", "--no_reports"):
            flags.no_reports = True
        elif a in ("--no-dispatcher", "--no_dispatcher"):
            flags.no_dispatcher = True
    return flags


def _install_signal_handlers(app: App) -> None:
    def handler(signum, _frame):
        app.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except Exception:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv
    app = App()
    flags = _parse_flags(argv)
    _install_signal_handlers(app)

    try:
        app.start(
            no_telegram=flags.no_telegram,
            no_reports=flags.no_reports,
            no_dispatcher=flags.no_dispatcher,
        )
        # Keep the main thread alive until stop_event is set
        while not app.stop_event.is_set():
            time.sleep(0.5)
        return 0
    except KeyboardInterrupt:
        app.stop()
        return 0
    except Exception as e:
        logging.getLogger("main").exception("Fatal error: %s", e)
        try:
            if notify_error:
                notify_error(f"fatal: {e}")
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
