# core/tz.py
# Timezone helpers

from __future__ import annotations
from datetime import datetime
from zoneinfo import ZoneInfo

def tz_init(tz_name: str = "UTC") -> ZoneInfo:
    """
    Επιστρέφει ZoneInfo object για τη ζητούμενη ζώνη ώρας.
    Default: UTC.
    """
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")

def now_dt(tz: ZoneInfo | None = None) -> datetime:
    """
    Επιστρέφει το τρέχον datetime με timezone.
    """
    tz = tz or ZoneInfo("UTC")
    return datetime.now(tz)

def ymd(dt: datetime | None = None) -> str:
    """
    Επιστρέφει ημερομηνία (YYYY-MM-DD) σαν string.
    Αν δεν δοθεί dt, χρησιμοποιεί τώρα UTC.
    """
    if dt is None:
        dt = datetime.now(ZoneInfo("UTC"))
    return dt.strftime("%Y-%m-%d")
