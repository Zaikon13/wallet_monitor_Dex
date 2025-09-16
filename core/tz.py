# core/tz.py
# -*- coding: utf-8 -*-
"""
Timezone helpers used by main.py
- now_local(): returns timezone-aware "now" using TZ env (default Europe/Athens)
- ymd(dt): YYYY-MM-DD string in local TZ
- parse_ts(s): parse "YYYY-MM-DD HH:MM:SS" to timezone-aware datetime
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

_DEF_TZ = os.getenv("TZ", "Europe/Athens")

def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(_DEF_TZ)
    except Exception:
        return ZoneInfo("UTC")

def now_local() -> datetime:
    """Return current datetime with the configured local timezone."""
    return datetime.now(_tz())

def ymd(dt: datetime | None = None) -> str:
    """Return YYYY-MM-DD in local timezone."""
    return (dt or now_local()).strftime("%Y-%m-%d")

def parse_ts(s: str, tz: ZoneInfo | None = None) -> datetime:
    """
    Parse 'YYYY-MM-DD HH:MM:SS' and return timezone-aware dt.
    If parsing fails, returns now_local().
    """
    try:
