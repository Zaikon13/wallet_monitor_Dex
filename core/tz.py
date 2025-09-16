from __future__ import annotations
import os, time
from datetime import datetime
from zoneinfo import ZoneInfo

def tz_init(tz_name: str = "UTC") -> ZoneInfo:
    """
    Initialize timezone settings. Sets TZ environment and returns ZoneInfo.
    """
    tz = tz_name or "UTC"
    os.environ["TZ"] = tz
    try:
        if hasattr(time, "tzset"):
            time.tzset()
    except Exception:
        pass
    try:
        return ZoneInfo(tz)
    except Exception:
        return ZoneInfo("UTC")

def now_dt(tz: ZoneInfo | None = None) -> datetime:
    """
    Returns current datetime with the given timezone (UTC if none provided).
    """
    tz = tz or ZoneInfo("UTC")
    return datetime.now(tz)

def ymd(dt: datetime | None = None) -> str:
    """
    Returns date string YYYY-MM-DD for given datetime (UTC if not provided).
    """
    if dt is None:
        dt = datetime.now(ZoneInfo("UTC"))
    return dt.strftime("%Y-%m-%d")
