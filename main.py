#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
codex_extract.py — Split a monolithic main.py into modular files.

Usage:
  python codex_extract.py --main ./main.py --out . --dry-run
  python codex_extract.py --main ./main.py --out . --write --backup
  python codex_extract.py --main ./main.py --out . --write --force

What it does:
- Parses main.py via AST and regex.
- Classifies functions/classes/constants into target modules based on names/docstrings.
- Writes modules under: core/, reports/, telegram/, utils/, tests/ (created if missing).
- Emits a plan with suggested import lines for the slimmed main.py.
- In --write mode, creates files with idempotent guards and appends code sections.
- NEVER deletes from main.py (no in-place editing); you review the plan and then prune.

Constraints:
- Pure Python, no external deps. Python 3.10+ recommended.
- Heuristics are conservative and leave ambiguous items in main.py, listing them in the plan.

# TODO: Optional - consider adding a --patch-main option to rewrite imports in-place.
"""

from __future__ import annotations
import argparse
import ast
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Tuple, Optional

# ---------------------- Configuration ----------------------

MODULE_MAP_RULES = [
    # (regex pattern on symbol name, target relative path)
    (r"(price|dex|wcro|usdt|get_price|HISTORY_LAST_PRICE)", "core/pricing.py"),
    (r"(rpc|cron(os)?_rpc|get_(native|erc20)_tx|fetch_.*_tx|provider)", "core/rpc.py"),
    (r"(holdings|snapshot|merge_holdings|get_wallet_snapshot|format_snapshot_lines)", "core/holdings.py"),
    (r"(watch|guard|tracked_pair|price_move|_last_prices|watchdog|throttle|backoff)", "core/watch.py"),
    (r"(alert|notify_error|notify_warning|send_alert)", "core/alerts.py"),
    (r"(aggregate|aggregates?|sum_per_(day|asset)|rolling|bucketize)", "reports/aggregates.py"),
    (r"(day_report|build_day_report_text|daily_report)", "reports/day_report.py"),
    (r"(ledger|cost_basis|update_cost_basis|append_ledger|fifo|realized|unrealized)", "reports/ledger.py"),
    (r"(escape_markdown|chunk|send_telegram|telegram_(api|send)|longpoll|offset|bot)", "telegram/api.py"),
    (r"(format_holdings|format_.*|render_|md_table|as_markdown)", "telegram/formatters.py"),
    (r"(safe_(get|json)|http|get_json|retry_request)", "utils/http.py"),
    (r"(tz|local_tz|get_tz|athens|to_local_time)", "core/tz.py"),
]

ALWAYS_CREATE = [
    "core/__init__.py",
    "reports/__init__.py",
    "telegram/__init__.py",
    "utils/__init__.py",
    "tests/__init__.py",
]

FILE_HEADERS: Dict[str, str] = {
    "core/pricing.py": "# core/pricing.py — extracted by codex_extract.py\n",
    "core/rpc.py": "# core/rpc.py — extracted by codex_extract.py\n",
    "core/holdings.py": "# core/holdings.py — extracted by codex_extract.py\n",
    "core/watch.py": "# core/watch.py — extracted by codex_extract.py\n",
    "core/alerts.py": "# core/alerts.py — extracted by codex_extract.py\n",
    "core/tz.py": "# core/tz.py — extracted by codex_extract.py\n",
    "reports/aggregates.py": "# reports/aggregates.py — extracted by codex_extract.py\n",
    "reports/day_report.py": "# reports/day_report.py — extracted by codex_extract.py\n",
    "reports/ledger.py": "# reports/ledger.py — extracted by codex_extract.py\n",
    "telegram/api.py": "# telegram/api.py — extracted by codex_extract.py\n",
    "telegram/formatters.py": "# telegram/formatters.py — extracted by codex_extract.py\n",
    "utils/http.py": "# utils/http.py — extracted by codex_extract.py\n",
}

GUARD_BEGIN = "# === BEGIN: extracted block (do not edit below) ==="
GUARD_END = "# === END: extracted block (do not edit above) ==="

MAIN_IMPORT_SUGGESTIONS = [
    # canonical imports that a slimmed main.py will likely need
    "from core.pricing import get_price_usd  # and any pricing helpers you used",
    "from core.rpc import get_native_tx, get_erc20_tx  # adjust to actual names",
    "from core.holdings import get_wallet_snapshot, format_snapshot_lines",
    "from core.watch import ensure_watchdogs_ready  # adjust to your watch helpers",
    "from core.alerts import notify_error, notify_warning",
    "from core.tz import local_tz",
    "from reports.day_report import build_day_report_text",
    "from reports.aggregates import aggregate_per_asset  # if used",
    "from reports.ledger import update_cost_basis, append_ledger  # if used",
    "from telegram.api import send_telegram_message, long_poll_loop  # if used",
    "from telegram.formatters import format_holdings, md_table  # if used",
    "from utils.http import safe_get, safe_json, get_json",
]

# ---------------------- Helpers ----------------------

def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")

def write_text_append_guarded(path: Path, payload: str, write: bool) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if GUARD_BEGIN in existing and GUARD_END in existing:
        # Append after last guard end to keep idempotency
        new_content = existing.rstrip() + "\n\n" + GUARD_BEGIN + "\n" + payload.rstrip() + "\n" + GUARD_END + "\n"
    else:
        header = FILE_HEADERS.get(str(path.relative_to(path.parents[0])), "")
        new_content = (header + "\n" if header and not existing else existing) + \
                      ("\n" if existing and not existing.endswith("\n") else "") + \
                      GUARD_BEGIN + "\n" + payload.rstrip() + "\n" + GUARD_END + "\n"
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_content, encoding="utf-8")

def classify_symbol(name: str, doc: str | None) -> Optional[str]:
    hay = name.lower()
    for pattern, target in MODULE_MAP_RULES:
        if re.search(pattern, hay):
            return target
    if doc:
        dlow = doc.lower()
        for pattern, target in MODULE_MAP_RULES:
            if re.search(pattern, dlow):
                return target
    return None  # leave in main.py

def get_source_segment(text: str, node: ast.AST) -> str:
    # ast.get_source_segment exists but we want resilient slicing:
    lines = text.splitlines(keepends=True)
    start = node.lineno - 1
    end = node.end_lineno
    return "".join(lines[start:end])

def collect_toplevel(text: str) -> Tuple[List[ast.AST], ast.Module]:
    tree = ast.parse(text)
    items = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign)):
            items.append(n)
    return items, tree

def is_constant_assign(node: ast.AST) -> bool:
    return isinstance(node, (ast.Assign, ast.AnnAssign))

def name_of(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    if isinstance(node, ast.Assign):
        # take first target when simple
        if node.targets and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
        return "assign"
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name):
            return node.target.id
        return "annassign"
    return "object"

def docstring_of(node: ast.AST) -> Optional[str]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return ast.get_docstring(node)
    return None

# ---------------------- Main logic ----------------------

def plan_extraction(main_path: Path, out_root: Path) -> Tuple[Dict[str, List[str]], List[str], List[str]]:
    """
    Returns:
      plan: mapping target_file -> list of code blocks
      ambiguous: symbol names left in main.py
      import_suggestions: lines to add to new main.py
    """
    text = read_text(main_path)
    items, _ = collect_toplevel(text)
    plan: Dict[str, List[str]] = {}
    ambiguous: List[str] = []

    for node in items:
        nm = name_of(node)
        doc = docstring_of(node)
        target = classify_symbol(nm, doc)
        seg = get_source_segment(text, node).rstrip()
        # Avoid moving "if __name__ == '__main__'" blocks or scheduler wiring
        if isinstance(node, ast.Assign):
            # constants: move only likely module constants
            if target is None:
                # try constant name hint
                if re.search(r"(HISTORY_LAST_PRICE|PRICE_CACHE|_last_prices|TZ|EOD_TIME|EPSILON|DEFAULT_RPC)", nm, re.I):
                    target = classify_symbol(nm, nm) or "core/pricing.py"
        if target:
            plan.setdefault(target, []).append(seg)
        else:
            ambiguous.append(nm)

    # ensure __init__ scaffolds exist in plan
    for init_rel in ALWAYS_CREATE:
        plan.setdefault(init_rel, plan.get(init_rel, []))

    return plan, ambiguous, MAIN_IMPORT_SUGGESTIONS.copy()

def write_plan(plan: Dict[str, List[str]], out_root: Path, write: bool) -> None:
    for rel, blocks in plan.items():
        rel = rel.replace("\\", "/")
        dest = out_root.joinpath(rel)
        if rel.endswith("__init__.py"):
            if write and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("# package init\n", encoding="utf-8")
            continue
        if rel not in FILE_HEADERS:
            # unknown file; still write with generic header
            FILE_HEADERS[rel] = f"# {rel} — extracted by codex_extract.py\n"
        if not blocks:
            # create empty file with header if missing
            if write and not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(FILE_HEADERS[rel], encoding="utf-8")
            continue
        payload = "\n\n".join(blocks).strip() + "\n"
        write_text_append_guarded(dest, payload, write=write)

def backup_main(main_path: Path) -> Path:
    backup = main_path.with_suffix(main_path.suffix + ".bak")
    shutil.copy2(main_path, backup)
    return backup

def main():
    ap = argparse.ArgumentParser(description="Extract symbols from main.py into modules.")
    ap.add_argument("--main", required=True, type=Path, help="Path to main.py")
    ap.add_argument("--out", required=True, type=Path, help="Project root (where core/, reports/, etc. live)")
    ap.add_argument("--dry-run", action="store_true", help="Plan only; do not write files")
    ap.add_argument("--write", action="store_true", help="Write files according to plan")
    ap.add_argument("--backup", action="store_true", help="Create main.py.bak before writing")
    ap.add_argument("--force", action="store_true", help="Proceed even if warnings detected")
    args = ap.parse_args()

    if not args.main.exists():
        raise SystemExit(f"[ERROR] main file not found: {args.main}")

    plan, ambiguous, import_suggestions = plan_extraction(args.main, args.out)

    print("=== Extraction Plan ===")
    for rel, blocks in plan.items():
        if rel.endswith("__init__.py"):
            continue
        print(f"- {rel}: {len(blocks)} block(s)")

    if ambiguous:
        print("\n=== Left in main.py (ambiguous / intentionally retained) ===")
        for nm in sorted(set(ambiguous)):
            print(f" * {nm}")

    print("\n=== Suggested imports for the slimmed main.py ===")
    for line in import_suggestions:
        print(line)

    if args.write:
        if args.backup:
            b = backup_main(args.main)
            print(f"\n[OK] Backup created: {b}")
        write_plan(plan, args.out, write=True)
        print("[OK] Files written.")
        if ambiguous and not args.force:
            print("\n[NOTE] Some symbols remain in main.py. Review the list above.")
    else:
        print("\n[DRY-RUN] No files written. Use --write to apply.")

if __name__ == "__main__":
    main()
