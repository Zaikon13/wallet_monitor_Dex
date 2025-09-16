#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cordex Diag — produces repo tree, hashes, env snapshot (masked), versions,
pip deps, and import checks, then writes:
- diag_tree.txt
- diag_imports.log
- diag_report.md
- diag_report.json
"""

from __future__ import annotations
import os, sys, json, hashlib, traceback, argparse, subprocess
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

MASK_KEYS = {
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ETHERSCAN_API",
    "RPC_URL", "WALLET_ADDRESS"
}
MASK_PARTIAL = {"WALLET_ADDRESS"}  # δείχνει μόνο αρχή+τέλος

# ---------------------------------------------------------------------

def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def walk_tree(base: str) -> list[dict]:
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        if any(x in dirpath for x in [".git", ".venv", "__pycache__", ".pytest_cache", ".github/artifacts"]):
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
    return "\n".join(f"{f['path']}  ({f['size']} bytes)  {f['sha256']}" for f in files)

def mask_value(k: str, v: str | None) -> str | None:
    if v is None:
        return None
    if k in MASK_KEYS:
        if k in MASK_PARTIAL and len(v) > 10:
            return v[:6] + "…" + v[-4:]
        return "****"
    return v

def collect_env(mask: bool) -> dict:
    # FIXED: τώρα είναι set.union αντί για list|set
    keys = sorted(set(MASK_KEYS).union({"TZ","DEX_PAIRS","PRICE_MOVE_THRESHOLD","INTRADAY_HOURS","EOD_TIME"}))
    return {k: (mask_value(k, os.getenv(k)) if mask else os.getenv(k)) for k in keys}

def run_cmd(args: list[str]) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=90)
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
                import importlib.util
                spec = importlib.util.spec_from_file_location("cordex_dynamic", os.path.join(ROOT, mod))
                m = importlib.util.module_from_spec(spec)
                assert spec and spec.loader
                spec.loader.exec_module(m)  # type: ignore
            else:
                __import__(mod)
            item["ok"] = True
        except Exception:
            item["traceback"] = traceback.format_exc()
        results.append(item)
    return results

def _write(path: str, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

# ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mask-secrets", action="store_true")
    ap.add_argument("--try-import", nargs="*", default=[])
    args = ap.parse_args()

    os.chdir(ROOT)
    error_tb = None

    try:
        files = walk_tree(ROOT)
        _write("diag_tree.txt", render_tree_txt(files))

        env_snap = collect_env(mask=args.mask_secrets)

        pyver = sys.version.replace("\n", " ")
        _, pip_freeze, pip_err = run_cmd([sys.executable, "-m", "pip", "freeze"])
        _, pipdeptree, pdt_err = run_cmd([sys.executable, "-m", "pipdeptree"])
        _, git_status, _ = run_cmd(["git", "status", "--porcelain"])

        imports = try_import_modules(args.try_import)
        _write("diag_imports.log", "\n".join(
            [f"[{it['target']}] OK={it['ok']}\n{it['traceback'] or ''}".strip() for it in imports]
        ))

        report = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "python": pyver,
            "files_count": len(files),
            "env": env_snap,
            "git_dirty": bool(git_status),
            "pip_freeze": (pip_freeze or "").splitlines(),
            "pipdeptree": pipdeptree,
            "imports": imports,
            "warnings": {
                "pipdeptree": pdt_err or None,
                "pip_freeze": pip_err or None,
            }
        }
    except Exception:
        error_tb = traceback.format_exc()
        report = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "python": sys.version.replace("\n"," "),
            "files_count": 0,
            "env": collect_env(mask=True),
            "git_dirty": None,
            "pip_freeze": [],
            "pipdeptree": "",
            "imports": [],
            "error": "Unhandled exception in cordex_diag.py",
        }

    # Markdown summary
    md = []
    md.append(f"**Time (UTC):** {report['timestamp']}")
    md.append(f"**Python:** `{report['python']}`")
    md.append(f"**Files scanned:** {report.get('files_count', 0)}")
    md.append(f"**Git dirty:** {('Yes' if report.get('git_dirty') else 'No') if report.get('git_dirty') is not None else 'N/A'}")
    md.append("")
    md.append("### Env (masked)")
    for k, v in (report.get("env") or {}).items():
        md.append(f"- **{k}**: `{v}`")
    md.append("")
    if report.get("warnings", {}).get("pipdeptree"):
        md.append(f"> pipdeptree warning: `{report['warnings']['pipdeptree']}`")
        md.append("")
    if error_tb:
        md.append("### ❌ Exception (traceback)")
        md.append("```")
        md.append(error_tb)
        md.append("```")
        md.append("")
    if report.get("imports"):
        md.append("### Import checks")
        for it in report["imports"]:
            md.append(f"- {'✅' if it['ok'] else '❌'} {it['target']}")
        md.append("")
    md.append("### Files (hashes) — see artifact or diag_tree.txt")

    _write("diag_report.md", "\n".join(md))
    with open("diag_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # πάντα επιτυχές exit για να μην κόβει το workflow
    sys.exit(0)

# ---------------------------------------------------------------------

if __name__ == "__main__":
    main()
