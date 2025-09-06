# sentinel/core/config.py
import os

def apply_env_aliases():
    """
    Συμφιλίωση ονομάτων ENV μεταξύ Railway και κώδικα.
    Αν υπάρχει το 'src' και λείπει το 'dst', αντιγράφεται.
    """
    aliases = {
        "ALERTS_INTERVAL_MINUTES": "ALERTS_INTERVAL_MIN",
        "DISCOVER_REQUIRE_WCRO_QUOTE": "DISCOVER_REQUIRE_WCRO",
        # μπορείς να προσθέσεις κι άλλα εδώ αν χρειαστεί
    }
    for src, dst in aliases.items():
        v = os.getenv(src)
        if v is not None and os.getenv(dst) is None:
            os.environ[dst] = v
