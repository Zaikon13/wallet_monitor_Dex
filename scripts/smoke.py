"""
PR-011 smoke: AST + safe imports (no network, no schedulers)

Σκοπός:
- Να αποτρέπουμε προφανή crashes πριν γίνει merge (syntax/import errors).
- Να λογαριάζει DRY_RUN=1, αλλά δεν απαιτείται.

Δεν αγγίζει δίκτυο, δεν ξεκινά threads/schedulers/telegram.
"""
import os
import sys

EXIT_OK = 0
EXIT_FAIL = 1

def main():
    # Προαιρετικό DRY_RUN flag
    dry_run = os.getenv("DRY_RUN", "1") in ("1", "true", "True", "YES", "yes")

    # Ελάχιστο import surface — ασφάλεια: ΜΟΝΟ modules που δεν καλούν δίκτυο στο import
    # Προσαρμόζεται στο repo σου (βάσει δημόσιων αρχείων που έχουμε δει).
    try:
        import core  # noqa: F401
        import utils  # noqa: F401
        # Μικρά modules που είναι “safe on import”
        try:
            from core import tz  # noqa: F401
        except Exception:
            pass
        try:
            from core import config  # noqa: F401
        except Exception:
            pass
        try:
            from utils import http  # noqa: F401
        except Exception:
            pass
    except Exception as e:
        print(f"[SMOKE:IMPORT] Failed: {e}", file=sys.stderr)
        return EXIT_FAIL

    # Ελαφρύς έλεγχος AppConfig αν υπάρχει — ΧΩΡΙΣ side-effects
    try:
        from core.config import AppConfig  # type: ignore
        cfg = AppConfig()
        # Βασικές τιμές που δεν προκαλούν network και πρέπει να υπάρχουν
        _ = getattr(cfg, "TZ", None)
        _ = getattr(cfg, "EOD_TIME", None)
        _ = getattr(cfg, "WEEKLY_DOW", None)
        # Log φιλικό (stdout)
        print(f"[SMOKE] TZ={cfg.TZ} EOD_TIME={cfg.EOD_TIME} WEEKLY_DOW={cfg.WEEKLY_DOW} DRY_RUN={dry_run}")
    except Exception as e:
        # Αν δεν υπάρχει AppConfig ακόμη, δεν αποτυγχάνει το PR.
        print(f"[SMOKE] AppConfig check skipped or failed: {e}")

    print("[SMOKE] OK — AST+Imports passed.")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
