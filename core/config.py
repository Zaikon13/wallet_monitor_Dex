# core/config.py
import os
from dotenv import load_dotenv

# Φόρτωσε .env αν υπάρχει (στο Railway αγνοείται, ok)
load_dotenv()

def _alias_env(src: str, dst: str):
    """Αν δεν υπάρχει το dst και υπάρχει το src, κάνε αντιγραφή — χρήσιμο για Railway ονόματα."""
    if os.getenv(dst) is None and os.getenv(src) is not None:
        os.environ[dst] = os.getenv(src)

def apply_env_aliases():
    # minutes vs min
    _alias_env("ALERTS_INTERVAL_MINUTES", "ALERTS_INTERVAL_MIN")
    # WCRO quote
    _alias_env("DISCOVER_REQUIRE_WCRO_QUOTE", "DISCOVER_REQUIRE_WCRO")
    # ενιαίο όριο απόλυτης μεταβολής (το χρησιμοποιείς ήδη)
    _alias_env("DISCOVER_MIN_ABS_CHANGE_PCT", "DISCOVER_MIN_ABS_CHANGE_PCT")
    # καθάρισε πιθανά κενά στα numeric envs
    for k in [
        "DEX_POLL","WALLET_POLL","INTRADAY_HOURS","EOD_HOUR","EOD_MINUTE",
        "PRICE_WINDOW","PRICE_MOVE_THRESHOLD","SPIKE_THRESHOLD",
        "ALERTS_INTERVAL_MIN","DISCOVER_LIMIT","DISCOVER_POLL",
        "DISCOVER_MIN_LIQ_USD","DISCOVER_MIN_VOL24_USD","DISCOVER_MAX_PAIR_AGE_HOURS",
        "PUMP_ALERT_24H_PCT","DUMP_ALERT_24H_PCT","MIN_VOLUME_FOR_ALERT",
        "GUARD_WINDOW_MIN","GUARD_PUMP_PCT","GUARD_DROP_PCT","GUARD_TRAIL_DROP_PCT",
    ]:
        v = os.getenv(k)
        if v is not None:
            os.environ[k] = v.strip()
