"""
ATR % levels from the trading-day open.

Run: python -m tests.test_atr_levels   (or pytest, if installed)
"""
from datetime import date

from desk.atr_levels import atr_levels, active_levels, clue, day_open_and_atr


def test_levels_match_indicator_math():
    lv = {x["label"]: x["price"] for x in atr_levels(30780, 472.9)}
    assert lv["ATR+25%"] == round(30780 + 472.9 * 0.25, 2)
    assert lv["ATR-75%"] == round(30780 - 472.9 * 0.75, 2)
    assert len(lv) == 8


def test_beyond_mode_only_uses_levels_outside_value():
    atr_lv = atr_levels(1000, 100)                       # 900..1100 in 25-pt steps
    user = [{"price": 960}, {"price": 1060}]
    used = {x["label"] for x in active_levels(atr_lv, user, "beyond")}
    assert used == {"ATR+75%", "ATR+100%", "ATR-50%", "ATR-75%", "ATR-100%"}
    assert active_levels(atr_lv, user, "off") == []
    assert len(active_levels(atr_lv, user, "all")) == 8


def test_clue_range_used_and_nearest_level():
    c = clue(1072, 1000, 100, atr_levels(1000, 100))
    assert c["range_pct"] == 72 and c["nearest"] == "ATR+75%" and c["nearest_dist"] == 3


def test_open_and_atr_use_only_completed_days():
    bars = [{"t": f"2026-09-{d:02d}T00:00:00+00:00", "o": 100, "h": 110, "l": 90, "c": 100} for d in range(1, 17)]
    bars.append({"t": "2026-09-17T00:00:00+00:00", "o": 105, "h": 500, "l": 0, "c": 105})   # huge day itself
    o, a = day_open_and_atr(bars, date(2026, 9, 17), 14)
    assert o == 105 and a == 20                        # the day's own range is not included


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
