"""
Stop / target / sizing rules and stop-vs-target simulation.

Run: python -m tests.test_risk   (or pytest, if installed)
"""
from desk.risk import RiskConfig, plan_trade, simulate

CFG = RiskConfig(point_value=2.0, min_contracts=2, max_contracts=3, max_risk_per_trade=250,
                 stop_buffer_pts=2.0, min_rr=1.5, min_target_pts=10.0)
LEVELS = [31461.75, 31252, 31079.25]


def test_short_off_resistance_targets_next_level_down():
    # Friday's 04:30 shooting star at 31252
    candle = {"o": 31229.25, "h": 31266.5, "l": 31219.5, "c": 31240.25}
    p = plan_trade(31252, {"bias": "bearish"}, candle, LEVELS, CFG)
    assert p["ok"] and p["side"] == "short"
    assert p["stop"] == 31268.5 and p["target"] == 31079.25
    assert p["contracts"] == 3 and p["risk_usd"] == 28.25 * 2 * 3


def test_long_off_support_targets_next_level_up():
    candle = {"o": 31081.25, "h": 31086, "l": 31061.5, "c": 31082}
    p = plan_trade(31079.25, {"bias": "bullish"}, candle, LEVELS, CFG)
    assert p["ok"] and p["stop"] == 31059.5 and p["target"] == 31252


def test_wide_stop_drops_to_two_contracts_then_skips():
    # 50 pt stop: 3 MNQ = $300 > $250, 2 MNQ = $200 -> 2 contracts
    candle = {"o": 31200, "h": 31248, "l": 31150, "c": 31200}
    p = plan_trade(31252, {"bias": "bearish"}, candle, LEVELS, CFG)
    assert p["ok"] and p["contracts"] == 2
    # 80 pt stop: even 2 MNQ = $320 -> skipped
    candle = {"o": 31170, "h": 31250, "l": 31150, "c": 31172}
    p = plan_trade(31252, {"bias": "bearish"}, candle, LEVELS + [30875], CFG)
    assert not p["ok"] and "stop too wide" in p["reason"]


def test_no_level_beyond_entry_skips():
    candle = {"o": 31070, "h": 31085, "l": 31060, "c": 31065}
    p = plan_trade(31079.25, {"bias": "bearish"}, candle, LEVELS, CFG)
    assert not p["ok"] and "no level" in p["reason"]


def test_target_skips_levels_too_close_for_min_rr():
    # Long at 100, stop 80 (20 pts): level at 110 is only 0.5R, 140 is 2R -> target 140
    candle = {"o": 95, "h": 101, "l": 82, "c": 100}
    p = plan_trade(90, {"bias": "bullish"}, candle, [90, 110, 140], CFG)
    assert p["ok"] and p["target"] == 140


def test_low_reward_risk_skips():
    # Target only 20 pts away with a 30 pt stop
    candle = {"o": 31090, "h": 31128, "l": 31085, "c": 31100}
    p = plan_trade(31120, {"bias": "bearish"}, candle, [31120, 31080], CFG)
    assert not p["ok"] and "1.5R" in p["reason"]


def test_wide_stop_falls_back_to_one_contract():
    cfg1 = RiskConfig(point_value=2.0, min_contracts=1, max_contracts=3, max_risk_per_trade=250,
                      stop_buffer_pts=2.0, min_rr=1.5, min_target_pts=10.0)
    # 80 pt stop: 2 MNQ = $320 too much, 1 MNQ = $160 fits
    candle = {"o": 31170, "h": 31250, "l": 31150, "c": 31172}
    p = plan_trade(31252, {"bias": "bearish"}, candle, LEVELS + [30875], cfg1)
    assert p["ok"] and p["contracts"] == 1 and p["risk_usd"] == 160


def test_simulate_stop_first_when_bar_hits_both():
    plan = {"side": "short", "entry": 100, "stop": 110, "target": 80, "contracts": 3}
    r = simulate(plan, [{"t": "t1", "o": 100, "h": 111, "l": 79, "c": 90}], CFG)
    assert r["how"] == "stop" and r["pnl_usd"] == -10 * 2 * 3


def test_simulate_target_and_time_exit():
    plan = {"side": "long", "entry": 100, "stop": 90, "target": 120, "contracts": 2}
    bars = [{"t": "t1", "o": 100, "h": 105, "l": 95, "c": 104}, {"t": "t2", "o": 104, "h": 121, "l": 103, "c": 118}]
    assert simulate(plan, bars, CFG)["how"] == "target"
    r = simulate(plan, bars[:1], CFG)
    assert r["how"] == "time" and r["pnl_pts"] == 4


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
