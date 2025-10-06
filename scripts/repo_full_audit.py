#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Repo Full Audit (static, no imports executed)

Checks:
- Syntax errors (AST)
- Missing internal modules (broken imports)
- Missing symbols (from X import Y not in X)
- Circular imports (graph cycles)
- Potential side-effects at import (top-level calls / getenv / schedule)
- scripts/: missing __main__ guard, missing argparse
- telegram/: lack of chunking (4096) & escaping helper usage

Outputs:
- console report
- repo_audit_report.md (markdown) at repo root (unless --no-file)

Usage:
  python scripts/repo_full_audit.py
  python scripts/repo_full_audit.py --no-file --format txt
"""
from __future__ import annotations
import ast, pathlib, sys, re, argparse
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
IGNORE_DIRS = {
    ".git",".github","__pycache__","venv",".venv","node_modules","dist","build",
    ".mypy_cache",".pytest_cache"
}
STD_PREFIX = {
    "sys","os","json","time","datetime","typing","pathlib","logging","math","re",
    "subprocess","itertools","functools","collections","decimal","asyncio","enum",
    "dataclasses","traceback","types","queue","signal","random","contextlib"
}
SIDE_EFFECT_HINT_LIBS = {"requests","httpx","schedule","threading","subprocess","socket"}

def iter_py_files(root: pathlib.Path):
    for p in root.rglob("*.py"):
        if any(part in IGNORE_DIRS for part in p.parts):
            continue
        yield p

def modname(p: pathlib.Path) -> str:
    return ".".join(p.relative_to(ROOT).with_suffix("").parts)

def parse_repo():
    idx, parsed, symbols, synerr = {}, {}, {}, []
    for p in iter_py_files(ROOT):
        m = modname(p)
        idx[m] = p
        try:
            src = p.read_text(encoding="utf-8")
            t = ast.parse(src, filename=str(p))
            parsed[m] = (t, src)
            # exported/defined names (shallow)
            defs, all_list = set(), None
            for n in getattr(t, "body", []):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    defs.add(n.name)
                elif isinstance(n, ast.Assign):
                    for tgt in n.targets:
                        if isinstance(tgt, ast.Name): defs.add(tgt.id)
                    # __all__
                    for tgt in getattr(n, "targets", []):
                        if isinstance(tgt, ast.Name) and tgt.id == "__all__" and isinstance(n.value, (ast.List, ast.Tuple)):
                            all_list = {
                                elt.value for elt in n.value.elts
                                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                            }
                elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                    defs.add(n.target.id)
            if all_list:
                defs |= all_list
            symbols[m] = defs
        except SyntaxError as e:
            synerr.append((str(p), f"{e.msg} (line {e.lineno}, col {e.offset})"))
    return idx, parsed, symbols, synerr

def is_std(name: str) -> bool:
    return name.split(".")[0] in STD_PREFIX

def resolve_from(cur_mod: str, node: ast.ImportFrom) -> str:
    pkg = node.module or ""
    level = node.level or 0
    if level == 0:
        return pkg
    base = cur_mod.split(".")[:-level]
    return ".".join(base + (pkg.split(".") if pkg else []))

def build_import_graph(idx, parsed, symbols):
    missing_modules, missing_symbols = [], []
    graph = defaultdict(set)
    sidefx = defaultdict(list)

    for mod, (tree, src) in parsed.items():
        # side-effects heuristics (top-level)
        for n in getattr(tree, "body", []):
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                # try to name the call
                func = n.value.func
                nm = None
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    nm = func.value.id
                elif isinstance(func, ast.Name):
                    nm = func.id
                if nm in SIDE_EFFECT_HINT_LIBS:
                    sidefx[str(idx[mod])].append(f"Top-level call using '{nm}'")
                else:
                    sidefx[str(idx[mod])].append("Top-level function call")
            if isinstance(n, ast.Assign) and isinstance(getattr(n, "value", None), ast.Call):
                sidefx[str(idx[mod])].append("Top-level call in assignment")
            # os.getenv / schedule.every hints
            if isinstance(n, ast.Assign) and isinstance(getattr(n, "value", None), ast.Call):
                val = n.value
                if isinstance(val.func, ast.Attribute) and isinstance(val.func.value, ast.Name):
                    if val.func.value.id == "os" and val.func.attr == "getenv":
                        sidefx[str(idx[mod])].append("os.getenv at import-time")
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
                f = n.value.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                    if f.value.id == "schedule" and f.attr == "every":
                        sidefx[str(idx[mod])].append("schedule.every(...) at import-time")

        # import analysis
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    name = a.name
                    if is_std(name): 
                        continue
                    # try resolve to internal module (prefix walk)
                    parts = name.split(".")
                    found = False
                    while parts:
                        cand = ".".join(parts)
                        if cand in idx:
                            graph[mod].add(cand)
                            found = True
                            break
                        parts.pop()
                    if not found and name.split(".")[0] in {k.split(".")[0] for k in idx}:
                        missing_modules.append((mod, name))
            elif isinstance(n, ast.ImportFrom):
                target = resolve_from(mod, n)
                if target:
                    exists = False
                    check = target
                    parts = check.split(".") if check else []
                    while True:
                        if check in idx:
                            graph[mod].add(check)
                            exists = True
                            break
                        if not parts:
                            break
                        parts.pop()
                        check = ".".join(parts) or ""
                    if not exists and target.split(".")[0] in {k.split(".")[0] for k in idx}:
                        missing_modules.append((mod, target))
                    if target in symbols:
                        defined = symbols[target]
                        for a in n.names:
                            if a.name != "*" and a.name not in defined:
                                missing_symbols.append((mod, target, a.name))
    return graph, missing_modules, missing_symbols, sidefx

def find_cycles(graph):
    seen, stack, out = set(), [], []
    def dfs(u):
        if u in stack:
            i = stack.index(u)
            out.append(stack[i:] + [u])
            return
        if u in seen: return
        seen.add(u); stack.append(u)
        for v in graph.get(u, ()):
            dfs(v)
        stack.pop()
    for node in list(graph.keys()):
        dfs(node)
    uniq, ded = [], set()
    for c in out:
        t = tuple(c)
        if t not in ded:
            ded.add(t); uniq.append(c)
    return uniq

def check_scripts_and_telegram(parsed):
    findings = []
    for mod, (tree, src) in parsed.items():
        path_str = str(mod)
        # scripts: __main__ guard & argparse
        if path_str.startswith("scripts."):
            if "if __name__ == '__main__':" not in src and 'if __name__ == "__main__":' not in src:
                findings.append(("[SCRIPTS_MAIN_GUARD]", mod, "missing __main__ guard"))
            if "argparse" not in src:
                findings.append(("[SCRIPTS_ARGPARSE]", mod, "argparse not used (CLI args likely hardcoded)"))
        # telegram hygiene
        if path_str.startswith("telegram."):
            uses_send = re.search(r"(send_message|send_telegram_message|bot\.send)", src, re.I) is not None
            if uses_send and ("4096" not in src and "chunk" not in src.lower()):
                findings.append(("[TELEGRAM_CHUNKING]", mod, "no explicit chunking safeguard (4096 chars)"))
            if uses_send and ("escape" not in src.lower()):
                findings.append(("[TELEGRAM_ESCAPE]", mod, "no escaping helper referenced (risk: parse entities)"))
    return findings

def format_report(data, fmt="md"):
    (synerr, miss_mod, miss_sym, sidefx, cycles, extra) = data
    lines = []
    push = lines.append
    if fmt == "md":
        push("# Repo Audit Report\n")
    else:
        push("REPO AUDIT REPORT\n")
    if synerr:
        push("## Syntax errors")
        for p, msg in synerr:
            push(f"- {p}: {msg}")
        push("")
    if miss_mod:
        push("## Missing internal modules (broken imports)")
        for where, name in sorted(set(miss_mod)):
            push(f"- {where}: `{name}`")
        push("")
    if miss_sym:
        push("## Missing symbols (from X import Y not found in X)")
        for where, target, sym in sorted(set(miss_sym)):
            push(f"- {where}: `{sym}` from `{target}`")
        push("")
    if sidefx:
        push("## Potential side-effects at import (heuristic)")
        for p, items in sidefx.items():
            for it in items:
                push(f"- {p}: {it}")
        push("")
    if cycles:
        push("## Circular imports")
        for cyc in cycles:
            push(f"- {' -> '.join(cyc)}")
        push("")
    if extra:
        push("## Scripts & Telegram hygiene")
        for tag, mod, msg in extra:
            push(f"- {tag} {mod}: {msg}")
        push("")
    if not any([synerr, miss_mod, miss_sym, sidefx, cycles, extra]):
        push("✅ No issues detected by static audit.")
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-file", action="store_true", help="Do not write repo_audit_report.md")
    ap.add_argument("--format", choices=["md","txt"], default="md", help="Report format")
    args = ap.parse_args()

    idx, parsed, symbols, synerr = parse_repo()
    graph, miss_mod, miss_sym, sidefx = build_import_graph(idx, parsed, symbols)
    cyc = find_cycles(graph)
    extra = check_scripts_and_telegram(parsed)

    report = format_report((synerr, miss_mod, miss_sym, sidefx, cyc, extra), fmt=args.format)
    print(report)
    if not args.no_file:
        out = ROOT / ("repo_audit_report.md" if args.format == "md" else "repo_audit_report.txt")
        out.write_text(report, encoding="utf-8")

if __name__ == "__main__":
    raise SystemExit(main())
