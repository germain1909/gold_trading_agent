"""
Level rejection / acceptance and candlestick pattern checks.

Run: python -m tests.test_patterns   (or pytest, if installed)
"""
from desk.patterns import evaluate_level

L = 100.0


def candle(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c}


def test_shooting_star_rejects_resistance():
    # Opened below, wicked up through 100, closed back below near the low
    r = evaluate_level(L, candle(97, 104, 96.5, 96.8))
    assert r["role"] == "resistance" and r["outcome"] == "rejected" and r["bias"] == "bearish"
    assert "shooting star" in r["patterns"]


def test_hammer_rejects_support():
    # Opened above, wicked down through 100, closed back above near the high
    r = evaluate_level(L, candle(103, 103.5, 96, 103.2))
    assert r["role"] == "support" and r["outcome"] == "rejected" and r["bias"] == "bullish"
    assert "hammer" in r["patterns"]


def test_touch_counts_as_test():
    # High exactly at the level, closed below: still a rejection
    r = evaluate_level(L, candle(98, 100, 97, 97.5))
    assert r["outcome"] == "rejected"


def test_close_beyond_is_acceptance():
    r = evaluate_level(L, candle(98, 103, 97.5, 102.5))
    assert r["role"] == "resistance" and r["outcome"] == "accepted" and r["bias"] == "bullish"


def test_bearish_engulfing_at_resistance():
    prev = candle(97, 99.5, 96.5, 99)          # green candle into the level
    cur = candle(99.25, 101, 96, 96.5)         # red candle engulfs it, closes below level
    r = evaluate_level(L, cur, prev)
    assert r["outcome"] == "rejected" and "bearish engulfing" in r["patterns"]


def test_wick_rejection_without_pattern():
    # Closed back below, but a big body and small wick: rejection, no pattern
    r = evaluate_level(L, candle(92, 100.5, 91.5, 99))
    assert r["outcome"] == "rejected" and r["patterns"] == []


def test_untouched_level_is_ignored():
    assert evaluate_level(L, candle(95, 99.75, 94, 99)) is None


def test_close_on_level_is_ignored():
    assert evaluate_level(L, candle(98, 101, 97, 100)) is None


def test_strength_grades():
    # Shooting star at resistance -> pattern
    assert evaluate_level(L, candle(97, 104, 96.5, 96.8))["strength"] == "pattern"
    # Upper wick is half the range, but the lower wick is too big for a shooting star -> wick
    assert evaluate_level(L, candle(96, 104, 92, 98))["strength"] == "wick"
    # Friday's 09:30 NQ candle vs 31170: opened 3 under, poked 10 through, dumped 133 -> weak
    r = evaluate_level(31170, candle(31167, 31180, 30995.5, 31036.5))
    assert r["outcome"] == "rejected" and r["strength"] == "weak"
    # Acceptances aren't graded
    assert evaluate_level(L, candle(98, 103, 97.5, 102.5))["strength"] == ""


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
