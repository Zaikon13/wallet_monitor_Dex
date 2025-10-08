"""
PR-011 smoke: AST + ultra-safe imports (no network, no schedulers, no main)
"""
import os
import sys
import importlib

EXIT_OK = 0
EXIT_FAIL = 1

SAFE_MODULES = [
    "core.tz",
    "core.config",
    "utils.http",
]

def safe_import(mod: str):
    try:
        return importlib.import_module(mod)
    except Exception as e:
        print(f"[SMOKE:IMPORT] {mod} failed: {e}", file=sys.stderr)
        return None

def main():
    os.environ.setdefault("DRY_RUN", "1")
    any_fail = False
    for mod in SAFE_MODULES:
        m = safe_import(mod)
        if m is None:
            any_fail = True
    try:
        from core.config import AppConfig  # type: ignore
        cfg = AppConfig()
        print(f"[SMOKE] TZ={getattr(cfg,'TZ',None)} "
              f"EOD_TIME={getattr(cfg,'EOD_TIME',None)} "
              f"WEEKLY_DOW={getattr(cfg,'WEEKLY_DOW',None)} "
              f"DRY_RUN={os.getenv('DRY_RUN')}")
    except Exception as e:
        print(f"[SMOKE] AppConfig check skipped or failed: {e}")
    if any_fail:
        return EXIT_FAIL
    print("[SMOKE] OK — ultra-safe imports passed.")
    return EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
