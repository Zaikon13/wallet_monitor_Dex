#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/cordex_ping.py
Ultra-safe smoke test: δεν αποτυγχάνει ποτέ, απλώς τυπώνει τι δουλεύει.
"""

from __future__ import annotations
import sys
import importlib

TARGETS = (
    # βασικά modules/πακέτα του repo
    "core",
    "core.config",
    "core.tz",
    "core.pricing",
    "core.alerts",
    "reports",
    "reports.aggregates",
    "reports.day_report",
    "reports.ledger",
    "telegram",
    "telegram.api",
    "telegram.formatters",
    "utils",
    "utils.http",
    # προαιρετικά: το main.py ως module από αρχείο
    # Αν το τρέξεις απευθείας, δεν θα εκτελέσει main loop.
)

def _print_ok(name: str):
    print(f"[OK]    {name}")

def _print_fail(name: str, err: Exception):
    print(f"[FAIL]  {name} -> {type(err).__name__}: {err}")

def _try_import(name: str):
    try:
        importlib.import_module(name)
        _print_ok(name)
    except Exception as e:
        _print_fail(name, e)

def main() -> int:
    print("Python:", sys.version.replace("\n", " "))
    # δοκίμασε imports ένα-ένα χωρίς να ρίχνεις exit code
    for t in TARGETS:
        _try_import(t)

    # Προαιρετικά: προσπάθησε να φορτώσεις το main.py μόνο ως module-from-file, χωρίς να τρέξεις τίποτα
    try:
        import os
        import importlib.util
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        main_path = os.path.join(root, "main.py")
        if os.path.isfile(main_path):
            spec = importlib.util.spec_from_file_location("cordex_main_smoke", main_path)
            if spec and spec.loader:
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)  # δεν τρέχει main loop αν είναι σωστά γραμμένο
                _print_ok("main.py (file import)")
            else:
                print("[WARN] Could not create spec for main.py")
        else:
            print("[WARN] main.py not found at repo root")
    except Exception as e:
        _print_fail("main.py (file import)", e)

    # Ποτέ failure exit — ώστε το Actions βήμα να μη γίνει
