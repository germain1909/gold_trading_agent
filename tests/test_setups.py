"""
Trend-cross and stretched-rejection setups, and the trailing exit.

Run: python -m tests.test_setups   (or pytest, if installed)
"""
from desk.risk import RiskConfig, simulate_trail
from desk.setups import trend_cross_signals, stretched_rejection_signals, trend_pullback_signals

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


def test_cross_filters_prior_candles_or_breakout():
    # Lines at 100/100, ATR 10. Three closes below both, then a small cross above -> prior rule passes
    below = [c(f"0{h}:00", 95, 96, 94, 95) for h in range(1, 4)]
    cross = c("04:00", 99, 103, 98, 102)                          # body 3 = 0.3 ATR
    lines, atr_v = [100] * 4, [10] * 4
    sigs = trend_cross_signals(below + [cross], lines, lines, 30, cfg=CFG, atr_v=atr_v,
                               min_prior=3, breakout_body_atr=1.5)
    assert sigs[-1]["qualified"]
    # Only one close below first: small cross is chop, a 1.6 ATR breakout candle still qualifies
    chop = [c("01:00", 101, 102, 100, 101), c("02:00", 99, 100, 94, 95)]
    small = trend_cross_signals(chop + [cross], [100] * 3, [100] * 3, 30, cfg=CFG, atr_v=[10] * 3,
                                min_prior=3, breakout_body_atr=1.5)
    assert not small[-1]["qualified"] and "other side" in small[-1]["why_not"]
    big = c("04:00", 95, 112, 94, 111)                            # body 16 = 1.6 ATR
    brk = trend_cross_signals(chop + [big], [100] * 3, [100] * 3, 30, cfg=CFG, atr_v=[10] * 3,
                              min_prior=3, breakout_body_atr=1.5)
    assert brk[-1]["qualified"]


def test_prior_rule_only_in_chosen_sessions():
    # Same small chop cross at 01:30 UTC (21:30 ET = Asia) and 14:30 UTC (10:30 ET = NY)
    def chop_then_cross(hour):
        return [c(f"{hour - 1:02d}:30", 101, 102, 100, 101), c(f"{hour:02d}:00", 99, 100, 94, 95),
                c(f"{hour:02d}:30", 99, 103, 98, 102)]
    kw = dict(cfg=CFG, atr_v=[10] * 3, min_prior=3, breakout_body_atr=1.5, prior_sessions=["London", "NY"])
    asia = trend_cross_signals(chop_then_cross(1), [100] * 3, [100] * 3, 30, **kw)
    ny = trend_cross_signals(chop_then_cross(14), [100] * 3, [100] * 3, 30, **kw)
    assert asia[-1]["qualified"] and not ny[-1]["qualified"]


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


def test_fade_needs_day_to_have_moved():
    candles = [c("01:00", 180, 190, 178, 188), c("01:30", 190, 206, 189, 191)]
    levels = [{"price": 200, "label": "VAH"}, {"price": 150, "label": "VAL"}]
    args = (candles, [150, 150], [140, 140], [20, 20], levels, 30, CFG)
    # day open 150, daily ATR 100: candle high 206 = +56% -> fade allowed at 50%
    assert stretched_rejection_signals(*args, day_open=150, day_atr=100, fade_min_range_pct=50)[0]["qualified"]
    # day open 170: high 206 = +36% -> too early
    early = stretched_rejection_signals(*args, day_open=170, day_atr=100, fade_min_range_pct=50)[0]
    assert not early["qualified"] and "too early" in early["why_not"]


def test_trend_pullback_hammer_in_uptrend():
    # Lines at 100; three closes above, then a hammer that dips to support 110 and closes above it
    candles = [c("01:00", 112, 118, 111, 117), c("01:30", 117, 121, 115, 120),
               c("02:00", 120, 121, 109, 120.5)]
    levels = [{"price": 110, "label": "pdVAH"}, {"price": 160, "label": "ATR+50%"}]
    sigs = trend_pullback_signals(candles, [100] * 3, [100] * 3, levels, 30, CFG, min_trend=3)
    assert len(sigs) == 1 and sigs[0]["side"] == "long" and sigs[0]["plan"]["target"] == 160
    # Without an established trend there is no signal
    assert trend_pullback_signals(candles, [100] * 3, [119] * 3, levels, 30, CFG, min_trend=3) == []


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
