# core/tz.py
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

# Θα οριστικοποιηθεί με tz_init() στην εκκίνηση
LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Europe/Athens"))

def tz_init(tz_str: str | None = None):
    """Κλειδώνει το timezone σε όλο το process (Europe/Athens by default)."""
    tz = (tz_str or os.getenv("TZ") or "Europe/Athens").strip()
    os.environ["TZ"] = tz
    try:
        if hasattr(time, "tzset"):
            time.tzset()
    except Exception:
        pass
    global LOCAL_TZ
    LOCAL_TZ = ZoneInfo(tz)
    return LOCAL_TZ

def now_dt():
    return datetime.now(LOCAL_TZ)

def ymd(dt=None):
    if dt is None: dt = now_dt()
    return dt.strftime("%Y-%m-%d")

def month_prefix(dt=None):
    if dt is None: dt = now_dt()
    return dt.strftime("%Y-%m")
