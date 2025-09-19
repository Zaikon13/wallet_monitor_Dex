# core/tz.py
"""
Timezone helpers.
- Use TZ from environment (default UTC)
- Provide now_local(), today_str(), datetime_str(), ymd()
"""

import os
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

# Read from env
_TZ_NAME = os.getenv("TZ", "UTC")

try:
    LOCAL_TZ = ZoneInfo(_TZ_NAME)
except Exception as e:
    logging.warning(f"Invalid TZ '{_TZ_NAME}', falling back to UTC ({e})")
    LOCAL_TZ = ZoneInfo("UTC")


def now_local() -> datetime:
    """
    Return current datetime in configured local TZ.
    """
    return datetime.now(LOCAL_TZ)


def today_str() -> str:
    """
    Return today's date as YYYY-MM-DD in local TZ.
    """
    return now_local().strftime("%Y-%m-%d")


def datetime_str(dt: datetime) -> str:
    """
    Format datetime as ISO string in local TZ.
    """
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")


def ymd(dt: datetime | None = None) -> str:
    """
    Return YYYY-MM-DD string from datetime (local TZ).
    If dt is None, use now().
    """
    if dt is None:
        dt = now_local()
    return dt.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
