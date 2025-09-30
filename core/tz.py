from datetime import datetime, timezone, timedelta
def now_gr(): return datetime.now(timezone(timedelta(hours=3)))
def ymd(dt=None): dt=dt or now_gr(); return dt.strftime("%Y-%m-%d")
