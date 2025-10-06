#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import json
import logging
import os
import platform
import socket
import sys
import time
from typing import Any, Dict, List

RESULT: Dict[str, Any] = {"ok": True, "checks": {}, "notes": []}


def _set(name: str, ok: bool, **meta) -> None:
    RESULT["checks"][name] = {"ok": bool(ok), **meta}
    if not ok:
        RESULT["ok"] = False


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or ""


def check_python() -> None:
    ver = sys.version.split()[0]
    _set("python.version", ver >= "3.10", version=ver)


def check_env_required() -> None:
    # προσαρμόζεις αν θέλεις αυστηρότερα
    required = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "WALLET_ADDRESS", "CRONOS_RPC_URL")
    missing = [k for k in required if not _env(k)]
    _set("env.required", len(missing) == 0, missing=missing)


def check_imports() -> None:
    mods = ["requests", "python_dotenv", "schedule", "flask"]
    status: Dict[str, Any] = {}
    ok = True
    for m in mods:
        try:
            mod = importlib.import_module(m.replace("-", "_"))
            ver = getattr(mod, "__version__", None)
            status[m] = {"import": True, "version": ver}
        except Exception as e:
            ok = False
            status[m] = {"import": False, "error": str(e)}
    _set("imports.runtime_libs", ok, libs=status)


def check_repo_modules() -> None:
    to_check = [
        "core.config",
        "core.tz",
        "core.rpc",
        "core.pricing",
        "core.holdings",
        "core.wallet_monitor",
        "core.guards",
        "core.watch",
        "core.providers.cronos",
        "core.signals.server",
        "reports.scheduler",
        "reports.aggregates",
        "telegram.api",
        "telegram.dispatcher",
    ]
    status: Dict[str, Any] = {}
    ok = True
    for m in to_check:
        try:
            importlib.import_module(m)
            status[m] = {"import": True}
        except Exception as e:
            ok = False
            status[m] = {"import": False, "error": str(e)}
    _set("imports.project_modules", ok, modules=status)


def check_rpc() -> None:
    import json as _json
    import urllib.request as req

    url = _env("CRONOS_RPC_URL")
    if not url:
        _set("network.rpc", False, error="CRONOS_RPC_URL missing")
        return
    payload = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode()
    try:
        r = req.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with req.urlopen(r, timeout=8) as resp:
            ok = resp.status == 200
            body = resp.read(256).decode(errors="ignore")
            _set("network.rpc", ok, sample=body)
    except Exception as e:
        _set("network.rpc", False, error=str(e))


def check_cronoscan() -> None:
    import urllib.parse as up
    import urllib.request as req

    base = _env("CRONOSSCAN_BASE") or "https://api.cronoscan.com/api"
    key = _env("CRONOSCAN_API") or _env("ETHERSCAN_API")
    q = {"module": "stats", "action": "cronowei"}
    if key:
        q["apikey"] = key
    url = base + "?" + up.urlencode(q)
    try:
        with req.urlopen(url, timeout=8) as r:
            ok = r.status == 200
            sample = r.read(160).decode(errors="ignore")
            _set("network.cronoscan", ok, url=base, sample=sample[:160])
    except Exception as e:
        _set("network.cronoscan", False, url=base, error=str(e))


def check_telegram() -> None:
    import urllib.request as req

    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        _set("network.telegram", False, error="TELEGRAM_BOT_TOKEN missing")
        return
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        with req.urlopen(url, timeout=8) as r:
            ok = r.status == 200
            _set("network.telegram", ok, status=r.status)
    except Exception as e:
        _set("network.telegram", False, error=str(e))


def check_signals_health() -> None:
    # θα πετύχει μόνο αν τρέχει ήδη ο HTTP server στο ίδιο container
    bind = _env("SIGNALS_BIND") or "0.0.0.0:8080"
    host, port = (bind.split(":", 1) + ["8080"])[:2]
    host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{host}:{port}/healthz"
    try:
        import urllib.request as req

        with req.urlopen(url, timeout=3) as r:
            ok = r.status == 200
            _set("network.signals_health", ok, url=url, status=r.status)
    except Exception as e:
        _set("network.signals_health", False, url=url, error=str(e))


def check_watch_and_monitor() -> None:
    ok = True
    info: Dict[str, Any] = {}

    # watcher
    try:
        mod = importlib.import_module("core.watch")
        mf = getattr(mod, "make_from_env", None)
        if callable(mf):
            w = mf()
            has_poll = hasattr(w, "poll_once")
            info["watcher"] = {"make_from_env": True, "has_poll_once": bool(has_poll)}
        else:
            ok = False
            info["watcher"] = {"make_from_env": False}
    except Exception as e:
        ok = False
        info["watcher"] = {"error": str(e)}

    # wallet monitor
    try:
        from core.wallet_monitor import make_wallet_monitor
        from core.providers.cronos import fetch_wallet_txs

        mon = make_wallet_monitor(provider=fetch_wallet_txs)
        info["wallet_monitor"] = {"make_wallet_monitor": True, "type": type(mon).__name__}
    except Exception as e:
        ok = False
        info["wallet_monitor"] = {"error": str(e)}

    _set("app.watch_and_monitor", ok, info=info)


def check_scheduler_bindings() -> None:
    ok = True
    info: Dict[str, Any] = {}
    try:
        from reports.scheduler import start_eod_scheduler, run_pending

        info["start_eod_scheduler"] = callable(start_eod_scheduler)
        info["run_pending"] = callable(run_pending)
        ok = info["start_eod_scheduler"] and info["run_pending"]
    except Exception as e:
        ok = False
        info["error"] = str(e)
    _set("app.scheduler_bindings", ok, info=info)


def check_dispatcher_contract() -> None:
    ok = True
    info: Dict[str, Any] = {}
    try:
        import telegram.dispatcher as d

        has_dispatch = callable(getattr(d, "dispatch", None))
        has__dispatch = callable(getattr(d, "_dispatch", None))
        info = {"dispatch": has_dispatch, "_dispatch": has__dispatch}
        ok = has_dispatch or has__dispatch
    except Exception as e:
        ok = False
        info = {"error": str(e)}
    _set("app.dispatcher_contract", ok, info=info)


def check_runtime_env() -> None:
    runtime = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "tz": _env("TZ") or "UTC",
        "railway": {
            "service": _env("RAILWAY_SERVICE_NAME"),
            "env": _env("RAILWAY_ENVIRONMENT"),
            "region": _env("RAILWAY_REGION"),
        },
    }
    _set("runtime.env", True, info=runtime)


def main(argv: List[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    check_runtime_env()
    check_python()
    check_env_required()
    check_imports()
    check_repo_modules()
    check_rpc()
    check_cronoscan()
    check_telegram()
    check_dispatcher_contract()
    check_scheduler_bindings()
    check_watch_and_monitor()
    check_signals_health()

    print(json.dumps(RESULT, ensure_ascii=False, indent=2))
    # Exit 0 if everything is ok, else 1 (useful for CI/Railway health)
    return 0 if RESULT["ok"] else 1


def cli() -> None:
    raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    pass
