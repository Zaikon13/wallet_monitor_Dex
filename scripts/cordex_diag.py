#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cordex Diag — produces repo tree, hashes, env snapshot (masked), versions,
pip deps, and import checks, then writes:
- diag_tree.txt
- diag_imports.log
- diag_report.md
- diag_report.json
Run:
  python scripts/cordex_diag.py --mask-secrets --try-import main.py core telegram reports utils
"""
from __future__ import annotations
import os, sys, json, hashlib, traceback, argparse, subprocess
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

MASK_KEYS = {
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ETHERSCAN_API",
    "RPC_URL", "WALLET_ADDRESS"
}
MASK_PARTIAL = {"WALLET_ADDRESS"}  # δείξε τα πρώτα/τελευταία 4

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def walk_tree(base: str) -> list[dict]:
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        # αγνόησε κλασικά build dirs
        if any(x in dirpath for x in [".git", ".venv", "__pycache__", ".pytest_cache"]):
            continue
        for fn in sorted(filenames):
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, base)
            try:
                size = os.path.getsize(p)
                hsh = sha256_file(p) if size < 5_000_000 else "skipped>5MB"
            except Exception:
                size, hsh = -1, "err"
            out.append({"path": rel, "size": size, "sha256": hsh})
    return sorted(out, key=lambda d: d["path"])

def render_tree_txt(files: list[dict]) -> str:
    lines = []
    for f in files:
        lines.append(f"{f['path']}  ({f['size']} bytes)  {f['sha256']}")
    return "\n".join(lines)

def mask_value(k: str, v: str) -> str:
    if not v:
        return v
    if k in MASK_KEYS:
        if k in MASK_PARTIAL and len(v) > 8:
            return v[:6] + "…" + v[-4:]
        return "****"
    return v

def collect_env(mask: bool) -> dict:
    keys = sorted(set(list(MASK_KEYS) | {
        "TZ", "DEX_PAIRS", "PRICE_MOVE_THRESHOLD", "INTRADAY_HOURS", "EOD_TIME"
    }))
    snap = {}
    for k in keys:
        v = os.getenv(k)
        snap[k] = mask_value(k, v) if mask else v
    return snap

def run_cmd(args: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"

def try_import_modules(mods: list[str]) -> list[dict]:
    results = []
    sys.path.insert(0, ROOT)
    for mod in mods:
        item = {"target": mod, "ok": False, "traceback": None}
        try:
            if mod.endswith(".py") and os.path.isfile(os.path.join(ROOT, mod)):
                # import from file
                import importlib.util
                spec = importlib.util.spec_from_file_location("cordex_dynamic", os.path.join(ROOT, mod))
                m = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(m)  # type: ignore
            else:
                __import__(mod)
            item["ok"] = True
        except Exception:
            item["traceback"] = traceback.format_exc()
        results.append(item)
    return results

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mask-secrets", action="store_true")
    ap.add_argument("--try-import", nargs="*", default=[])
    args = ap.parse_args()

    os.chdir(ROOT)

    files = walk_tree(ROOT)
    tree_txt = render_tree_txt(files)
    with open("diag_tree.txt", "w", encoding="utf-8") as f:
        f.write(tree_txt)

    env_snap = collect_env(mask=args.mask_secrets)

    pyver = sys.version.replace("\n", " ")
    rc, pip_freeze, _ = run_cmd([sys.executable, "-m", "pip", "freeze"])
    rc2, pipdeptree, _ = run_cmd([sys.executable, "-m", "pipdeptree"])
    rc3, git_status, _ = run_cmd(["git", "status", "--porcelain"])

    imports = try_import_modules(args.try_import)
    with open("diag_imports.log", "w", encoding="utf-8") as f:
        for it in imports:
            f.write(f"[{it['target']}] OK={it['ok']}\n")
            if it["traceback"]:
                f.write(it["traceback"] + "\n")

    report = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "python": pyver,
        "files_count": len(files),
        "env": env_snap,
        "git_dirty": bool(git_status),
        "pip_freeze": pip_freeze.splitlines() if pip_freeze else [],
        "imports": imports,
    }
    with open("diag_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # Markdown summary
    ok_failed = [
        ("✅", it["target"]) if it["ok"] else ("❌", it["target"]) for it in imports
    ]
    imports_md = "\n".join([f"- {m} {t}" for m, t in ok_failed]) or "_(no targets)_"

    md = []
    md.append(f"**Time (UTC):** {report['timestamp']}")
    md.append(f"**Python:** `{pyver}`")
    md.append(f"**Files scanned:** {len(files)}")
    md.append(f"**Git dirty:** {'Yes' if report['git_dirty'] else 'No'}")
    md.append("")
    md.append("### Env (masked)")
    for k, v in report["env"].items():
        md.append(f"- **{k}**: `{v}`")
    md.append("")
    md.append("### Import checks")
    md.append(imports_md)
    md.append("")
    md.append("### Pip freeze (top 20)")
    top20 = report["pip_freeze"][:20]
    md.extend([f"- `{line}`" for line in top20])
    md.append("")
    md.append("### Files (hashes) — see artifact or diag_tree.txt")
    md_text = "\n".join(md)

    with open("diag_report.md", "w", encoding="utf-8") as f:
        f.write(md_text)

    print("Wrote: diag_tree.txt, diag_imports.log, diag_report.md, diag_report.json")

if __name__ == "__main__":
    main()
