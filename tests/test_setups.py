"""
Trend-cross and stretched-rejection setups, and the trailing exit.

Run: python -m tests.test_setups   (or pytest, if installed)
"""
from desk.risk import RiskConfig, simulate_trail
from desk.setups import trend_cross_signals, stretched_rejection_signals

CFG = RiskConfig(point_value=2.0, min_contracts=2, max_contracts=3, max_risk_per_trade=250,
                 stop_buffer_pts=2.0, min_rr=1.5, min_target_pts=10.0)


def c(t, o, h, l, cl):
    return {"t": f"2026-10-09T{t}:00+00:00", "o": o, "h": h, "l": l, "c": cl}


def test_trend_cross_long_only_on_fresh_cross():
    candles = [c("01:00", 100, 101, 99, 100), c("01:30", 100, 111, 99, 110), c("02:00", 110, 113, 109, 112)]
    ema_l, basis = [101, 102, 103], [104, 104, 105]
    sigs = trend_cross_signals(candles, ema_l, basis, 30, cfg=CFG)
    assert [(s["i"], s["side"]) for s in sigs] == [(1, "long")]     # 2nd close above isn't a new cross
    plan = sigs[0]["plan"]
    assert plan["stop"] == 100 and plan["entry"] == 110             # min(102, 104) - 2


def test_stretched_rejection_needs_stretch_and_pattern():
    # Shooting star into a level at 200, far above lines at ~150 with ATR 20 -> 2.5 ATR stretched
    candles = [c("01:00", 180, 190, 178, 188), c("01:30", 190, 206, 189, 191)]
    levels = [{"price": 200, "label": "VAH"}, {"price": 150, "label": "VAL"}]
    sigs = stretched_rejection_signals(candles, [150, 150], [140, 140], [20, 20], levels, 30, CFG)
    assert len(sigs) == 1 and sigs[0]["side"] == "short" and sigs[0]["qualified"]
    # Same candle but lines right underneath -> not stretched
    sigs = stretched_rejection_signals(candles, [185, 185], [186, 186], [20, 20], levels, 30, CFG)
    assert not sigs[0]["qualified"] and "stretch" in sigs[0]["why_not"]


def test_trail_exits_on_close_back_across_ema():
    plan = {"side": "long", "entry": 100, "stop": 90, "contracts": 3}
    candles = [
        {**c("02:00", 100, 108, 99, 107), "end": "2026-10-09T02:30:00+00:00"},
        {**c("02:30", 107, 109, 101, 102), "end": "2026-10-09T03:00:00+00:00"},   # closes below EMA 104
    ]
    r = simulate_trail(plan, [], candles, {candles[0]["t"]: 103, candles[1]["t"]: 104}, CFG)
    assert r["how"] == "trail" and r["exit"] == 102 and r["pnl_usd"] == 2 * 2 * 3


def test_trail_hard_stop_on_minute_bars():
    plan = {"side": "short", "entry": 100, "stop": 110, "contracts": 2}
    candles = [{**c("02:00", 100, 112, 95, 99), "end": "2026-10-09T02:30:00+00:00"}]
    minutes = [{"t": "2026-10-09T02:05:00+00:00", "o": 100, "h": 111, "l": 99, "c": 108}]
    r = simulate_trail(plan, minutes, candles, {candles[0]["t"]: 105}, CFG)
    assert r["how"] == "stop" and r["pnl_usd"] == -10 * 2 * 2


def test_trail_exits_at_level_target():
    plan = {"side": "long", "entry": 100, "stop": 90, "target": 120, "contracts": 2}
    candles = [{**c("02:00", 100, 125, 99, 124), "end": "2026-10-09T02:30:00+00:00"}]
    minutes = [{"t": "2026-10-09T02:10:00+00:00", "o": 110, "h": 121, "l": 109, "c": 120}]
    r = simulate_trail(plan, minutes, candles, {candles[0]["t"]: 95}, CFG)
    assert r["how"] == "target" and r["pnl_usd"] == 20 * 2 * 2


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
