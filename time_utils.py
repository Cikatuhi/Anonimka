from datetime import datetime
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

def now_msk():
    """Текущее московское время с таймзоной (timezone-aware)"""
    return datetime.now(MSK)