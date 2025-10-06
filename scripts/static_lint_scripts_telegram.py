import ast, pathlib, sys, re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TARGETS = [ROOT / "scripts", ROOT / "telegram"]
REPORT = ROOT / "partial_health_scripts_telegram.txt"

def py_files(d):
    return [p for p in d.rglob("*.py") if p.is_file()]

STD = {"sys","os","pathlib","argparse","json","time","logging","typing","dataclasses","decimal","re","collections","itertools","functools","datetime","math","asyncio"}
RISKS = {"requests","httpx","schedule","threading","subprocess","socket"}

findings = []

def check_main_guard(path, src):
    ok = ("if __name__ == \"__main__\":" in src) or ("if __name__ == '__main__':" in src)
    if not ok:
        findings.append(f"[MAIN_GUARD] {path}: missing if __name__ == '__main__'")

def check_argparse_required(path, src):
    # μόνο για scripts/
    if "scripts" not in str(path.parent):
        return
    if "argparse" not in src:
        findings.append(f"[ARGPARSE] {path}: argparse not used (CLI params likely hardcoded)")

def check_top_level_calls(path, tree):
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            findings.append(f"[TOP_LEVEL_CALL] {path}: function call at import-time")
        if isinstance(node, ast.Assign) and isinstance(getattr(node, "value", None), ast.Call):
            findings.append(f"[TOP_LEVEL_CALL] {path}: assignment from call at import-time")

def check_risky_imports(path, tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in RISKS:
                    findings.append(f"[RISKY_IMPORT] {path}: '{root}' imported (ensure no top-level usage)")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in RISKS:
                    findings.append(f"[RISKY_IMPORT] {path}: from '{root}' import ... (ensure no top-level usage)")

def check_telegram_hygiene(path, src):
    # heuristics: chunking & escape presence in telegram files
    if "telegram" not in str(path):
        return
    if re.search(r"send_message|send_telegram_message|bot\.send", src, re.I):
        if "4096" not in src and "chunk" not in src.lower():
            findings.append(f"[TELEGRAM_CHUNKING] {path}: no explicit chunking safeguard for 4096 chars")
        # crude escape detection
        if "escape" not in src.lower():
            findings.append(f"[TELEGRAM_ESCAPE] {path}: no escaping helper referenced (risk: 'can’t parse entities')")

def scan_file(p):
    try:
        src = p.read_text(encoding="utf-8")
    except Exception as e:
        findings.append(f"[READ_FAIL] {p}: {e}")
        return
    try:
        tree = ast.parse(src, filename=str(p))
    except SyntaxError as e:
        findings.append(f"[SYNTAX] {p}: {e.msg} (line {e.lineno})")
        return
    # generic checks
    check_top_level_calls(p, tree)
    check_risky_imports(p, tree)
    # per-area checks
    if "/scripts/" in str(p).replace("\\","/"):
        check_main_guard(p, src)
        check_argparse_required(p, src)
    if "/telegram/" in str(p).replace("\\","/"):
        check_telegram_hygiene(p, src)

def main():
    for d in TARGETS:
        if d.exists():
            for f in py_files(d):
                scan_file(f)
        else:
            findings.append(f"[MISSING_DIR] {d}")

    report = ["# Partial Health Report — scripts/ & telegram/", ""]
    if findings:
        report += sorted(findings)
    else:
        report.append("✅ No issues detected in scripts/ and telegram/ by static heuristics.")
    text = "\n".join(report)
    print(text)
    REPORT.write_text(text, encoding="utf-8")

if __name__ == "__main__":
    sys.exit(main())
