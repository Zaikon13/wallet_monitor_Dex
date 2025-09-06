# sentinel/core/tz.py
import os, time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo(os.getenv("TZ", "Europe/Athens"))

def init_tz(tz_str: str | None):
    """
    Θέτει OS TZ, κάνει tzset (όπου υποστηρίζεται) και ορίζει ZoneInfo.
    """
    global LOCAL_TZ
    tz = tz_str or "Europe/Athens"
    os.environ["TZ"] = tz
    try:
        if hasattr(_time, "tzset"):
            _time.tzset()
    except Exception:
        pass
    LOCAL_TZ = ZoneInfo(tz)

def now_dt():
    return datetime.now(LOCAL_TZ)

def ymd(dt=None):
    if dt is None: dt = now_dt()
    return dt.strftime("%Y-%m-%d")

def month_prefix(dt=None):
    if dt is None: dt = now_dt()
    return dt.strftime("%Y-%m")
