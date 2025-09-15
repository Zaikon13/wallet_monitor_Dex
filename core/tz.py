# core/tz.py
from datetime import datetime
from zoneinfo import ZoneInfo
import os, time

_DEF_TZ = os.getenv("TZ", "Europe/Athens")

def tz_init(tz_str: str | None = None) -> ZoneInfo:
    tz = tz_str or _DEF_TZ
    os.environ["TZ"] = tz
    if hasattr(time, "tzset"):  # type: ignore
        try:
            time.tzset()  # type: ignore[attr-defined]
        except Exception:
            pass
    return ZoneInfo(tz)

LOCAL_TZ = tz_init(_DEF_TZ)

def now_dt():
    return datetime.now(LOCAL_TZ)

def ymd(dt=None):
    return (dt or now_dt()).strftime("%Y-%m-%d")

def month_prefix(dt=None):
    return (dt or now_dt()).strftime("%Y-%m")
