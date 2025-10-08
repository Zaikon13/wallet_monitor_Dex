#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AST diagnostic + conservative auto-repair:
- Walk all *.py (excl .venv, __pycache__)
- Try ast.parse; on failure attempt safe fixes:
  * Detect "global 4-space shift" -> remove exactly 4 leading spaces on every line
  * Strip BOM, normalize CRLF to LF
  * Remove leading blank lines before module docstring/imports
- Re-parse; if ok, write fixed content; else keep original and record failure.
Emits a machine-friendly report.
"""
from __future__ import annotations
import ast, pathlib, sys

ROOT = pathlib.Path(".")
EXCLUDE_PARTS = {".venv", "__pycache__"}

def is_excluded(p: pathlib.Path) -> bool:
    return any(part in EXCLUDE_PARTS for part in p.parts)

def read_text(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")

def write_text(p: pathlib.Path, s: str) -> None:
    p.write_text(s, encoding="utf-8", newline="\n")

def looks_globally_indented(lines: list[str]) -> bool:
    nonempty = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith(('"""',"'''"))][:30]
    if not nonempty:
        return False
    # If none of the first non-empty lines start at col 0 and most start with 4 spaces
    return all(ln.startswith("    ") for ln in nonempty)

def safe_dedent4(s: str) -> str:
    return "\n".join(ln[4:] if ln.startswith("    ") else ln for ln in s.splitlines())

def normalize(s: str) -> str:
    # strip UTF-8 BOM, normalize CRLF
    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    # remove leading empty lines
    while s.startswith("\n\n"):
        s = s[1:]
    return s

def try_parse(src: str, path: str) -> tuple[bool, str]:
    try:
        ast.parse(src, filename=path)
        return True, ""
    except SyntaxError as e:
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"

def main() -> int:
    bad, fixed = [], []
    for p in ROOT.rglob("*.py"):
        if is_excluded(p):
            continue
        src = read_text(p)
        ok, msg = try_parse(src, str(p))
        if ok:
            continue

        orig = src
        src = normalize(src)
        lines = src.splitlines()
        # attempt dedent if it looks like module is globally indented
        if looks_globally_indented(lines):
            src = safe_dedent4(src)

        ok2, msg2 = try_parse(src, str(p))
        if ok2:
            if src != orig:
                write_text(p, src)
                fixed.append(str(p))
        else:
            bad.append((str(p), msg, msg2))

    # Report
    if fixed:
        print("[AST-REPAIR] Fixed files:")
        for f in fixed:
            print(f"  - {f}")
    if bad:
        print("[AST-FAIL] Still failing:")
        for f, m1, m2 in bad:
            print(f"  - {f}\n      before: {m1}\n      after : {m2}")
        # Non-zero exit to make CI show failures
        return 2
    print("AST repair sweep OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
