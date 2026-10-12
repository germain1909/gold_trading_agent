"""
Market brief for planning a trading day: every timeframe, momentum, profiles and levels.

    python -m desk.market_brief 2026-10-13            # brief for planning Tuesday 10/13
    python -m desk.market_brief 2026-10-13 31370      # plus your own levels, checked for confluence

Prints:
  - monthly / weekly / daily / 4H / 1H / 15m: last bars with RSI(14), MACD(12,26,9), EMA20/50, and a one-line read
  - volume profiles (VAH / POC / VAL) of the last 5 trading days and last week's composite
  - previous week high / low, daily ATR(14), estimated ATR % lines from the latest price
  - confluences: levels from different sources within 25 points of each other

Everything uses data available now; if the prior session hasn't closed yet, its profile is marked provisional.
Monthly indicators need ~26 months of history, which a single futures contract doesn't have.
"""
import sys
from collections import OrderedDict
from datetime import datetime, date, timedelta, timezone
from typing import Dict, Any, List, Optional
from zoneinfo import ZoneInfo

from topstep.client import TopstepClient
from .atr_levels import fetch_daily_bars, PCTS
from .backtest import front_month_id
from .indicators import ema, atr, rsi, macd
from .profile import volume_profile, session_window
from .replay import post_with_retry, fetch_bars, ET

UTC = ZoneInfo("UTC")
CONFLUENCE_PTS = 25


def _bars(client, cid: str, unit: int, n: int, start: datetime, end: datetime) -> List[Dict[str, Any]]:
    client._ensure_token()
    r = post_with_retry(client, client._base_url + "/api/History/retrieveBars", {
        "contractId": cid, "live": False,
        "startTime": start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "endTime": end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "unit": unit, "unitNumber": n, "limit": 5000, "includePartialBar": True}).json()
    return sorted(r.get("bars") or [], key=lambda b: b["t"])


def _aggregate(daily: List[Dict[str, Any]], key) -> List[Dict[str, Any]]:
    out: "OrderedDict[Any, Dict[str, Any]]" = OrderedDict()
    for b in daily:
        k = key(datetime.fromisoformat(b["t"]).date())
        m = out.setdefault(k, {"t": str(k), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]})
        m["h"], m["l"], m["c"] = max(m["h"], b["h"]), min(m["l"], b["l"]), b["c"]
    return list(out.values())


def _f(x, d=0):
    return "n/a" if x is None else f"{x:,.{d}f}"


def timeframe(name: str, bars: List[Dict[str, Any]], show: int, label) -> None:
    cl = [b["c"] for b in bars]
    r, (ml, ms, mh), e20, e50 = rsi(cl), macd(cl), ema(cl, 20), ema(cl, 50)
    print(f"\n## {name}")
    for i in range(max(0, len(bars) - show), len(bars)):
        b = bars[i]
        trend = "" if mh[i] is None or i == 0 or mh[i - 1] is None else (" rising" if mh[i] > mh[i - 1] else " falling")
        print(f"  {label(b):>12}  O {_f(b['o'],2)} H {_f(b['h'],2)} L {_f(b['l'],2)} C {_f(b['c'],2)} | "
              f"RSI {_f(r[i],1)} | MACD {_f(ml[i],1)} sig {_f(ms[i],1)} hist {_f(mh[i],1)}{trend} | "
              f"EMA20 {_f(e20[i])} EMA50 {_f(e50[i])}")
    i = len(bars) - 1
    notes = []
    if e20[i] is not None and e50[i] is not None:
        c = cl[i]
        notes.append("above both EMAs" if c > max(e20[i], e50[i]) else "below both EMAs" if c < min(e20[i], e50[i])
                     else "between the EMAs")
    if r[i] is not None:
        notes.append("RSI overbought" if r[i] >= 70 else "RSI oversold" if r[i] <= 30 else f"RSI {r[i]:.0f}")
    if mh[i] is not None:
        side = "MACD above signal" if mh[i] > 0 else "MACD below signal"
        if i and mh[i - 1] is not None and (mh[i] > 0) != (mh[i - 1] > 0):
            side += " (just crossed)"
        notes.append(side + (", histogram rising" if i and mh[i - 1] is not None and mh[i] > mh[i - 1]
                             else ", histogram falling"))
    print("  read: " + "; ".join(notes) if notes else "  read: not enough history")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    plan_day = datetime.strptime(args[0], "%Y-%m-%d").date()
    extra = [float(a.replace(",", "")) for a in args[1:]]
    symbol = "NQ"
    client = TopstepClient()
    cid = front_month_id(symbol, plan_day)
    now = datetime.now(timezone.utc)

    daily = [b for b in fetch_daily_bars(client, cid, plan_day, lookback_days=400)
             if datetime.fromisoformat(b["t"]).date() < plan_day]
    days = [datetime.fromisoformat(b["t"]).date() for b in daily]
    # the session before plan_day may still be running: include it from intraday data if so
    prev_day = plan_day - timedelta(days=1)
    while prev_day.weekday() >= 5:
        prev_day -= timedelta(days=1)
    s_prev, e_prev = session_window(prev_day, "full")
    provisional = now < e_prev
    if prev_day not in days and now > s_prev:
        live = _bars(client, cid, 2, 30, s_prev, now)
        if live:
            daily.append({"t": f"{prev_day.isoformat()}T00:00:00+00:00", "o": live[0]["o"],
                          "h": max(b["h"] for b in live), "l": min(b["l"] for b in live), "c": live[-1]["c"]})
            days.append(prev_day)
    last = daily[-1]["c"]

    print(f"# Market brief for {plan_day} ({cid}) - generated {now.astimezone(ET):%a %m/%d %H:%M} ET")
    print(f"Last price {last:,.2f}" + (f" | the {prev_day} session is still open: its levels are PROVISIONAL" if provisional else ""))

    lbl_d = lambda b: datetime.fromisoformat(b["t"]).strftime("%m/%d") if "T" in b["t"] else b["t"]
    timeframe("Monthly (one contract's history - indicators need ~26 months)",
              _aggregate(daily, lambda d: d.strftime("%Y-%m")), 4, lambda b: b["t"])
    timeframe("Weekly", _aggregate(daily, lambda d: d - timedelta(days=d.weekday())), 4, lambda b: b["t"])
    timeframe("Daily", daily, 5, lbl_d)
    lbl_i = lambda b: datetime.fromisoformat(b["t"]).astimezone(ET).strftime("%m/%d %H:%M")
    timeframe("4H", _bars(client, cid, 3, 4, now - timedelta(days=60), now), 6, lbl_i)
    timeframe("1H", _bars(client, cid, 3, 1, now - timedelta(days=20), now), 6, lbl_i)
    timeframe("15m", _bars(client, cid, 2, 15, now - timedelta(days=6), now), 6, lbl_i)

    day_atr = atr(daily, 14)[-1]
    print(f"\n## Levels\nDaily ATR(14): {day_atr:,.1f}")
    levels: List[tuple] = []
    for d in days[-5:]:
        bars = fetch_bars(client, cid, *session_window(d, "full"), minutes=1)
        if not bars:
            continue
        p = volume_profile(bars)
        tag = "pd" if d == days[-1] else d.strftime("%m/%d ")
        flag = " (provisional)" if d == prev_day and provisional else ""
        print(f"  {d:%a %m/%d}{flag}: VAH {p['vah']:,.2f}  POC {p['poc']:,.2f}  VAL {p['val']:,.2f}")
        for k in ("vah", "poc", "val"):
            levels.append((p[k], f"{tag}{k.upper()}"))

    this_week = plan_day - timedelta(days=plan_day.weekday())
    prev_week = [b for b in daily if this_week - timedelta(days=7) <= datetime.fromisoformat(b["t"]).date() < this_week]
    if prev_week:
        wh, wl = max(b["h"] for b in prev_week), min(b["l"] for b in prev_week)
        print(f"  Previous week: high {wh:,.2f}  low {wl:,.2f}")
        levels += [(wh, "prev week high"), (wl, "prev week low")]
        wk_bars = []
        for b in prev_week:
            wk_bars += fetch_bars(client, cid, *session_window(datetime.fromisoformat(b["t"]).date(), "full"), minutes=1)
        if wk_bars:
            p = volume_profile(wk_bars)
            print(f"  Previous week composite: VAH {p['vah']:,.2f}  POC {p['poc']:,.2f}  VAL {p['val']:,.2f}")
            levels += [(p["vah"], "week VAH"), (p["poc"], "week POC"), (p["val"], "week VAL")]

    e20 = ema([b["c"] for b in daily], 20)[-1]
    if e20:
        levels.append((e20, "daily EMA20"))
    atr_lines = [(last + day_atr * p / 100 * s, f"~ATR{'+' if s > 0 else '-'}{p}%") for p in PCTS for s in (1, -1)]
    print("  Estimated ATR lines (from the last price; the desk uses the real open): " +
          ", ".join(f"{lbl} {px:,.0f}" for px, lbl in sorted(atr_lines, reverse=True)))
    levels += atr_lines + [(x, "your level") for x in extra]

    print(f"\n## Confluences (within {CONFLUENCE_PTS} pts, different sources)")
    levels.sort()
    found = False
    for i, (p, lbl) in enumerate(levels):
        group = [(q, m) for q, m in levels[i:] if q - p <= CONFLUENCE_PTS]
        if len(group) > 1 and (i == 0 or p - levels[i - 1][0] > CONFLUENCE_PTS):
            found = True
            print(f"  {group[0][0]:,.0f}-{group[-1][0]:,.0f}: " + ", ".join(f"{m} {q:,.2f}" for q, m in group))
    if not found:
        print("  none")
    if extra:
        for x in extra:
            near = min((abs(q - x), m) for q, m in levels if m != "your level")
            print(f"  your level {x:,.2f}: nearest other level {near[1]} ({near[0]:,.1f} pts away)")


if __name__ == "__main__":
    main()
