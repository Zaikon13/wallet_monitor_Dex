#!/usr/bin/env python3
# Static health scan: syntax, broken imports/symbols, cycles, side-effects
import os, ast, pathlib, sys
from collections import defaultdict

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORT = ROOT / "repo_health_report.txt"
IGNORE = {".git",".github","__pycache__","venv",".venv","dist","build","node_modules"}

def iter_py():
    for p in ROOT.rglob("*.py"):
        if any(part in IGNORE for part in p.parts): continue
        yield p

def modname(p): return ".".join(p.relative_to(ROOT).with_suffix("").parts)

def parse_all():
    idx, parsed, symbols, synerr = {}, {}, {}, []
    for p in iter_py():
        m = modname(p); idx[m]=p
        try:
            t = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
            parsed[m]=t
            # symbols
            s=set()
            for n in getattr(t,"body",[]):
                if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)): s.add(n.name)
                if isinstance(n,ast.Assign):
                    for tgt in n.targets:
                        if isinstance(tgt,ast.Name): s.add(tgt.id)
            symbols[m]=s
        except SyntaxError as e:
            synerr.append((str(p), f"{e.msg} (line {e.lineno}, col {e.offset})"))
    return idx, parsed, symbols, synerr

def analyze(idx, parsed, symbols):
    missing_mod, missing_sym, sidefx = [], [], defaultdict(list)
    graph = defaultdict(set)
    def is_std(name): return name.split(".")[0] in {"sys","os","json","time","datetime","typing","pathlib","logging","math","re","subprocess","itertools","functools","collections","decimal","asyncio","enum","dataclasses","traceback","types","queue","signal","random"}
    def resolve(cur, pkg, level):
        if not level: return pkg or ""
        base = cur.split(".")[:-level]
        return ".".join(base + (pkg.split(".") if pkg else []))
    for mod, tree in parsed.items():
        # side-effects (top-level calls)
        for n in getattr(tree,"body",[]):
            if isinstance(n,ast.Expr) and isinstance(n.value,ast.Call):
                sidefx[str(idx[mod])].append("Top-level function call at import")
            if isinstance(n,ast.Assign) and isinstance(getattr(n,"value",None),ast.Call):
                sidefx[str(idx[mod])].append("Top-level call in assignment at import")
        # imports
        for n in ast.walk(tree):
            if isinstance(n,ast.Import):
                for a in n.names:
                    if is_std(a.name): continue
                    parts=a.name.split("."); found=False
                    while parts:
                        cand=".".join(parts)
                        if cand in idx: graph[mod].add(cand); found=True; break
                        parts.pop()
                    if not found and a.name.split(".")[0] in {k.split(".")[0] for k in idx}:
                        missing_mod.append((mod,a.name))
            if isinstance(n,ast.ImportFrom):
                target = resolve(mod, n.module or "", n.level or 0)
                if target:
                    check=target; parts=check.split(".")
                    exists=False
                    while True:
                        if check in idx: graph[mod].add(check); exists=True; break
                        if not parts: break
                        parts.pop(); check=".".join(parts) or ""
                    if not exists and target.split(".")[0] in {k.split(".")[0] for k in idx}:
                        missing_mod.append((mod,target))
                    if target in parsed:
                        defined=symbols.get(target,set())
                        for a in n.names:
                            if a.name!="*" and a.name not in defined:
                                missing_sym.append((mod,target,a.name))
    return graph, missing_mod, missing_sym, sidefx

def cycles(graph):
    seen, stack, out=set(),[],[]
    def dfs(u):
        if u in stack:
            i=stack.index(u); out.append(stack[i:]+[u]); return
        if u in seen: return
        seen.add(u); stack.append(u)
        for v in graph.get(u,()): dfs(v)
        stack.pop()
    for n in list(graph.keys()): dfs(n)
    # unique
    uniq=[]; ded=set()
    for c in out:
        t=tuple(c)
        if t not in ded: ded.add(t); uniq.append(c)
    return uniq

def main():
    idx, parsed, symbols, synerr = parse_all()
    graph, miss_mod, miss_sym, sidefx = analyze(idx, parsed, symbols)
    cyc = cycles(graph)
    lines=["# Repo Health Report",""]
    if synerr:
        lines.append("## Syntax errors"); lines += [f"- {p}: {m}" for p,m in synerr]; lines.append("")
    if miss_mod:
        lines.append("## Missing internal imports"); lines += [f"- {w}: `{n}`" for w,n in sorted(set(miss_mod))]; lines.append("")
    if miss_sym:
        lines.append("## Missing symbols"); lines += [f"- {w}: `{s}` from `{t}`" for w,t,s in sorted(set(miss_sym))]; lines.append("")
    if sidefx:
        lines.append("## Potential side-effects at import"); 
        for p,items in sidefx.items():
            for it in items: lines.append(f"- {p}: {it}")
        lines.append("")
    if cyc:
        lines.append("## Circular imports"); lines += [f"- {' -> '.join(c)}" for c in cyc]; lines.append("")
    if not any([synerr,miss_mod,miss_sym,sidefx,cyc]):
        lines.append("✅ No issues detected.")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))

def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    pass
