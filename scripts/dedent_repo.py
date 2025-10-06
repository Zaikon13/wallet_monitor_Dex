#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dedent Repo (safe)
- Εντοπίζει αρχεία .py που ξεκινούν με leading indentation (π.χ. πρώτο non-empty line ξεκινά με space/tab)
- Κάνει textwrap.dedent στο ΠΛΗΡΕΣ περιεχόμενο ΜΟΝΟ όταν:
  * Οι πρώτες 20 μη-κενές γραμμές είναι όλες indented (spaces/tabs)
- Dry-run by default: δείχνει τι ΘΑ αλλάξει. Με --write γράφει αλλαγές.

Usage:
  python scripts/dedent_repo.py            # μόνο report
  python scripts/dedent_repo.py --write    # γράφει αλλαγές
"""
from __future__ import annotations
import pathlib, sys, textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
IGNORE = {".git",".github","__pycache__","venv",".venv","node_modules","dist","build",".mypy_cache",".pytest_cache"}

def iter_py_files():
    for p in ROOT.rglob("*.py"):
        if any(part in IGNORE for part in p.parts): 
            continue
        yield p

def is_indented_line(line: str) -> bool:
    return line and (line[0] == " " or line[0] == "\t")

def looks_globally_indented(src: str, sample: int = 20) -> bool:
    # πάρε τις πρώτες sample μη-κενές γραμμές
    lines = [ln for ln in src.splitlines() if ln.strip()][:sample]
    if not lines:
        return False
    # true αν ΟΛΕΣ οι (έως sample) μη-κενές είναι indented
    return all(is_indented_line(ln) for ln in lines)

def process(path: pathlib.Path, write: bool = False):
    try:
        src = path.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[READ_FAIL] {path}: {e}")
        return False
    if not looks_globally_indented(src):
        return False
    ded = textwrap.dedent(src)
    if ded == src:
        return False
    if write:
        path.write_text(ded, encoding="utf-8")
        print(f"[FIXED] {path}")
    else:
        print(f"[WOULD_FIX] {path}")
    return True

def main():
    write = "--write" in sys.argv
    changed = 0
    for p in iter_py_files():
        if process(p, write=write):
            changed += 1
    print(f"\nSummary: {'fixed' if write else 'would fix'} {changed} file(s).")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
