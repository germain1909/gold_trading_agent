"""
Volume profile levels (POC, VAH, VAL) from 1-minute bars.

Each minute's volume is spread evenly across the price rows it traded
through. POC is the row with the most volume. The value area grows out from
the POC, adding whichever neighbouring row has more volume, until it holds
VALUE_AREA (70%) of the session's volume. VAH/VAL are its top and bottom.

Sessions (ET):
    full  - CME trading day, 18:00 the day before to 17:00
    rth   - NY cash session, 09:30 to 16:00
"""
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Dict, Any, List
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROW_SIZE = float(os.getenv("PROFILE_ROW_SIZE", 1.0))      # points per profile row
VALUE_AREA = float(os.getenv("PROFILE_VALUE_AREA", 0.70))


def session_window(day: date, session: str):
    if session == "rth":
        return (datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30),
                datetime.combine(day, datetime.min.time(), ET).replace(hour=16))
    return (datetime.combine(day - timedelta(days=1), datetime.min.time(), ET).replace(hour=18),
            datetime.combine(day, datetime.min.time(), ET).replace(hour=17))


def volume_profile(bars: List[Dict[str, Any]], row: float = ROW_SIZE, value_area: float = VALUE_AREA) -> Dict[str, float]:
    if not bars:
        raise ValueError("no bars for profile")
    vol: Dict[int, float] = defaultdict(float)
    for b in bars:
        v = float(b.get("v") or 0)
        lo, hi = int(b["l"] // row), int(b["h"] // row)
        n = hi - lo + 1
        for r in range(lo, hi + 1):
            vol[r] += v / n
    total = sum(vol.values())
    poc = max(vol, key=lambda r: (vol[r], -abs(r)))
    lo = hi = poc
    inside = vol[poc]
    rows_min, rows_max = min(vol), max(vol)
    while inside < value_area * total and (lo > rows_min or hi < rows_max):
        up = vol.get(hi + 1, 0.0) if hi < rows_max else -1.0
        dn = vol.get(lo - 1, 0.0) if lo > rows_min else -1.0
        if up >= dn:
            hi += 1
            inside += up
        else:
            lo -= 1
            inside += dn
    centre = row / 2
    return {
        "poc": round(poc * row + centre, 2),
        "vah": round(hi * row + row, 2),
        "val": round(lo * row, 2),
        "volume": total,
    }
