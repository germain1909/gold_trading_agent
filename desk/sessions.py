"""
Trading sub-sessions (US Eastern time). The trader takes at most one trade
in each of Asia, London and New York.

    Asia    18:00 - 03:00
    London  03:00 - 09:30
    NY      09:30 - 17:00
    (17:00 - 18:00 is the CME daily break)
"""
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SESSIONS = ["Asia", "London", "NY"]


def session_of(dt: datetime) -> Optional[str]:
    """Session name for a timezone-aware datetime, or None during the daily break."""
    t = dt.astimezone(ET).time()
    if t >= time(18, 0) or t < time(3, 0):
        return "Asia"
    if t < time(9, 30):
        return "London"
    if t < time(17, 0):
        return "NY"
    return None
