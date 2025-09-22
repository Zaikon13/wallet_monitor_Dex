from datetime import datetime
from zoneinfo import ZoneInfo

ATHENS = ZoneInfo("Europe/Athens")

def now_gr():
    return datetime.now(ATHENS)

def ymd():
    return now_gr().strftime("%Y-%m-%d")
