#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ultra-safe smoke test for repo module imports."""
from __future__ import annotations

import argparse
import importlib
import importlib.util
import os
import sys
from typing import Iterable

TARGETS = (
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
)


def _print_ok(name: str) -> None:
    print(f"[OK]    {name}")


def _print_fail(name: str, err: Exception) -> None:
    print(f"[FAIL]  {name} -> {type(err).__name__}: {err}")


def _try_import(name: str) -> None:
    try:
        importlib.import_module(name)
        _print_ok(name)
    except Exception as exc:  # pragma: no cover - diagnostic output only
        _print_fail(name, exc)


def _iter_targets(extra: Iterable[str]) -> Iterable[str]:
    seen = set()
    for item in TARGETS:
        if item not in seen:
            seen.add(item)
            yield item
    for item in extra:
        if item and item not in seen:
            seen.add(item)
            yield item


def _import_main_file(root: str) -> None:
    main_path = os.path.join(root, "main.py")
    if not os.path.isfile(main_path):
        print("[WARN] main.py not found at repo root")
        return
    spec = importlib.util.spec_from_file_location("cordex_main_smoke", main_path)
    if not spec or not spec.loader:
        print("[WARN] Could not create spec for main.py")
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    _print_ok("main.py (file import)")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cordex ping import smoke test")
    parser.add_argument(
        "--target",
        action="append",
        dest="targets",
        default=[],
        help="Additional dotted module path to import (can repeat).",
    )
    parser.add_argument(
        "--skip-main",
        action="store_true",
        help="Skip importing main.py as a module-from-file.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print("Python:", sys.version.replace("\n", " "))

    for target in _iter_targets(args.targets):
        _try_import(target)

    if not args.skip_main:
        try:
            root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
            _import_main_file(root)
        except Exception as exc:  # pragma: no cover - diagnostics only
            _print_fail("main.py (file import)", exc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
