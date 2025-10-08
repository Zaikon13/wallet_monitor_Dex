#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import ast, pathlib, sys

ROOT = pathlib.Path(".")
EXCLUDE = {".venv", "__pycache__"}

def excluded(p: pathlib.Path) -> bool:
    return any(part in EXCLUDE for part in p.parts)

def norm(s: str) -> str:
    if s.startswith("\ufeff"):
        s = s.lstrip("\ufeff")
    s = s.replace("\r\n","\n").replace("\r","\n")
    while s.startswith("\n\n"):
        s = s[1:]
    return s

def looks_globally_indented(lines: list[str]) -> bool:
    first = [ln for ln in lines if ln.strip()][:30]
    return bool(first) and all(ln.startswith("    ") for ln in first)

def dedent4(s: str) -> str:
    return "\n".join(ln[4:] if ln.startswith("    ") else ln for ln in s.splitlines())

def parse_ok(s: str, fname: str) -> tuple[bool,str]:
    try:
        ast.parse(s, filename=fname)
        return True,""
    except SyntaxError as e:
        return False,f"{type(e).__name__}: {e}"
    except Exception as e:
        return False,f"{type(e).__name__}: {e}"

def main() -> int:
    fixed, bad = [], []
    for p in ROOT.rglob("*.py"):
        if excluded(p): continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        ok, msg = parse_ok(src, str(p))
        if ok: continue

        orig = src
        s = norm(src)
        if looks_globally_indented(s.splitlines()):
            s = dedent4(s)

        ok2, msg2 = parse_ok(s, str(p))
        if ok2 and s != orig:
            p.write_text(s, encoding="utf-8", newline="\n")
            fixed.append(str(p))
        elif not ok2:
            bad.append((str(p), msg, msg2))

    if fixed:
        print("[AST-REPAIR] Fixed:")
        for f in fixed: print("  -", f)
    if bad:
        print("[AST-FAIL] Unfixed:")
        for f,m1,m2 in bad:
            print(f"  - {f}\n      before: {m1}\n      after : {m2}")
        return 2
    print("AST repair sweep OK")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
