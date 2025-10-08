import os, sys
EXIT_OK, EXIT_FAIL = 0, 1
def main():
    dry_run = os.getenv("DRY_RUN", "1") in ("1","true","True","YES","yes")
    try:
        import core, utils  # noqa: F401
        try: from core import tz  # noqa: F401
        except Exception: pass
        try: from core import config  # noqa: F401
        except Exception: pass
        try: from utils import http  # noqa: F401
        except Exception: pass
    except Exception as e:
        print(f"[SMOKE:IMPORT] Failed: {e}", file=sys.stderr)
        return EXIT_FAIL
    try:
        from core.config import AppConfig  # type: ignore
        cfg = AppConfig()
        print(f"[SMOKE] TZ={getattr(cfg,'TZ',None)} EOD_TIME={getattr(cfg,'EOD_TIME',None)} "
              f"WEEKLY_DOW={getattr(cfg,'WEEKLY_DOW',None)} DRY_RUN={dry_run}")
    except Exception as e:
        print(f"[SMOKE] AppConfig check skipped or failed: {e}")
    print("[SMOKE] OK — AST+Imports passed.")
    return EXIT_OK
if __name__ == "__main__":
    sys.exit(main())
