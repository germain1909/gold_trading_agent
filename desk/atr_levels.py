"""
ATR % levels from the trading-day open - the same levels as the trader's
TradingView indicator ("Daily History: ATR % from Open"):

    open  = first bar of the CME trading day (18:00 ET, after the NY close)
    ATR   = daily ATR(14) of the completed trading days before it
    level = open +/- ATR x 25 / 50 / 75 / 100 %

Modes (ATR_LEVELS):
    off     - not used as levels (default)
    beyond  - only the ATR levels outside the trader's own levels are used, so
              they only come into play once price leaves the value area
    all     - every ATR level is used alongside the trader's levels

Either way, clue() describes a price relative to the ATR levels so signals
can be annotated without changing any trade.
"""
import os
from datetime import date, datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .indicators import atr

PCTS = [25, 50, 75, 100]
ATR_DAYS = int(os.getenv("ATR_LEVELS_LENGTH", 14))
MODE = os.getenv("ATR_LEVELS", "off").lower()


def fetch_daily_bars(client, contract_id: str, day: date, lookback_days: int = 160) -> List[Dict[str, Any]]:
    """Topstep daily bars (one per CME trading day, labeled with the trading date)."""
    client._ensure_token()
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), ZoneInfo("UTC"))
    start = end - timedelta(days=lookback_days)
    from .replay import post_with_retry
    resp = post_with_retry(client, f"{client._base_url}/api/History/retrieveBars",
                           {"contractId": contract_id, "live": False,
                            "startTime": start.isoformat().replace("+00:00", "Z"),
                            "endTime": end.isoformat().replace("+00:00", "Z"),
                            "unit": 4, "unitNumber": 1, "limit": 1000, "includePartialBar": False})
    return sorted(resp.json().get("bars", []) or [], key=lambda b: b["t"])


def day_open_and_atr(daily_bars: List[Dict[str, Any]], day: date, length: int = ATR_DAYS) -> Tuple[float, float]:
    """Open of the trading day and ATR of the completed days before it."""
    idx = next((i for i, b in enumerate(daily_bars) if b["t"].startswith(day.isoformat())), None)
    if idx is None or idx < length:
        raise ValueError(f"Not enough daily bars before {day} for ATR({length})")
    return daily_bars[idx]["o"], atr(daily_bars[:idx], length)[-1]


def atr_levels(day_open: float, atr_value: float) -> List[Dict[str, Any]]:
    out = []
    for p in PCTS:
        out.append({"price": round(day_open + atr_value * p / 100, 2), "label": f"ATR+{p}%", "kind": "atr", "pct": p})
        out.append({"price": round(day_open - atr_value * p / 100, 2), "label": f"ATR-{p}%", "kind": "atr", "pct": -p})
    return sorted(out, key=lambda lv: lv["price"])


def active_levels(atr_lv: List[Dict[str, Any]], user_levels: List[Dict[str, Any]], mode: str = MODE) -> List[Dict[str, Any]]:
    """The ATR levels to trade with, given the mode."""
    if mode == "all":
        return list(atr_lv)
    if mode == "beyond" and user_levels:
        hi = max(lv["price"] for lv in user_levels)
        lo = min(lv["price"] for lv in user_levels)
        return [lv for lv in atr_lv if lv["price"] > hi or lv["price"] < lo]
    return []


def clue(price: float, day_open: float, atr_value: float, atr_lv: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Where a price sits: % of the day's ATR from the open, and the nearest ATR level."""
    nearest = min(atr_lv, key=lambda lv: abs(lv["price"] - price))
    return {
        "range_pct": round((price - day_open) / atr_value * 100),
        "nearest": nearest["label"],
        "nearest_dist": round(abs(nearest["price"] - price), 2),
    }


def describe(c: Dict[str, Any]) -> str:
    return f"day {c['range_pct']:+d}% of ATR, nearest {c['nearest']} {c['nearest_dist']:g} pts"
