"""Timezone utilities and date helpers."""
import os
from zoneinfo import ZoneInfo
from datetime import datetime


def init_tz(tz_str: str | None = None) -> ZoneInfo:
    tz = tz_str or os.getenv("TZ", "Europe/Athens")
    os.environ["TZ"] = tz
    try:
        import time as _t
        if hasattr(_t, "tzset"):
            _t.tzset()
    except Exception:
        pass
    return ZoneInfo(tz)


def ymd(dt: datetime | None = None, tz: str | None = None) -> str:
    """Return YYYY-MM-DD in the given (or process) timezone."""
    zone = ZoneInfo(tz or os.getenv("TZ", "Europe/Athens"))
    return (dt or datetime.now(zone)).strftime("%Y-%m-%d")
