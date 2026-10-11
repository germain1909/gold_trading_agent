"""
Trigger detector: each scenario type on small hand-built candle sequences.

Run: python -m tests.test_detector   (or pytest, if installed)
"""
from datetime import datetime, timedelta, timezone

from desk.detector import Context, ScenarioWatch
from desk.risk import RiskConfig
from desk.scenarios import from_dict

CFG = RiskConfig(point_value=2.0, min_contracts=1, max_contracts=3, max_risk_per_trade=250,
                 stop_buffer_pts=2.0, min_rr=1.5, min_target_pts=10.0)
NY_START = datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc)      # 10:00 ET
ASIA_START = datetime(2026, 10, 12, 0, 0, tzinfo=timezone.utc)     # 20:00 ET (Sunday evening)


def ctx_for(ohlc, ema=100.0, basis=100.0, atr=20.0, levels=None, start=NY_START, **kw):
    candles = [{"t": (start + timedelta(minutes=30 * k)).isoformat(), "o": o, "h": h, "l": l, "c": c}
               for k, (o, h, l, c) in enumerate(ohlc)]
    n = len(candles)
    as_list = lambda x: list(x) if isinstance(x, (list, tuple)) else [x] * n
    return Context(candles, as_list(ema), as_list(basis), as_list(atr), levels or {}, cfg=CFG, **kw)


def run(sc_dict, ctx):
    w = ScenarioWatch(from_dict(sc_dict), ctx.levels)
    for i in range(len(ctx.candles)):
        sig = w.on_candle(ctx, i)
        if sig:
            return sig, w
    return None, w


def test_reclaim_needs_a_dip_then_a_close_back_above():
    sc = {"name": "r", "type": "reclaim", "side": "long", "level": "pdVAL", "target": 120}
    lv = {"pdVAL": 100.0}
    sig, _ = run(sc, ctx_for([(105, 106, 103, 104), (104, 104, 98, 99), (99, 103, 98.5, 102)], levels=lv))
    p = sig["plan"]
    assert sig["i"] == 2 and p["ok"] and p["entry"] == 102 and p["stop"] == 96 and p["target"] == 120
    assert p["contracts"] == 3 and "after trading to 98" in sig["detail"]
    # never trades below -> never triggers
    sig, _ = run(sc, ctx_for([(105, 106, 103, 104), (104, 108, 101, 107)], levels=lv))
    assert sig is None


def test_breakout_needs_momentum():
    sc = {"name": "b", "type": "breakout", "side": "long", "level": 100, "target": 140}
    sig, _ = run(sc, ctx_for([(97, 100, 96, 99), (99.5, 112, 99, 111)]))
    assert sig["plan"]["ok"] and sig["plan"]["stop"] == 98            # below the level and the candle midpoint
    sig, _ = run(sc, ctx_for([(97, 100, 96, 99), (99.5, 103, 99, 102)]))  # body 2.5 < half ATR
    assert sig is None


def test_retest_after_break_and_failed_break_resets():
    sc = {"name": "rt", "type": "retest", "side": "long", "level": 100, "target": 140}
    sig, _ = run(sc, ctx_for([(97, 100, 96, 99), (99.5, 112, 99, 111), (111, 112, 99.5, 108)]))
    assert sig["i"] == 2 and sig["plan"]["stop"] == 97.5
    sig, _ = run(sc, ctx_for([(97, 100, 96, 99), (99.5, 112, 99, 111), (111, 111, 95, 96),
                              (96, 104, 95, 103)]))                       # closed back below: break failed
    assert sig is None


def test_trap_is_a_failed_break():
    sc = {"name": "t", "type": "trap", "side": "long", "level": 100, "target": 130}
    sig, _ = run(sc, ctx_for([(102, 103, 100.5, 101), (101, 101, 88, 89), (89, 102, 87, 101)]))
    assert sig["i"] == 2 and sig["plan"]["stop"] == 85 and "went to 87" in sig["detail"]
    sig, _ = run(sc, ctx_for([(102, 103, 100.5, 101), (101, 101, 88, 89), (89, 92, 85, 90),
                              (90, 92, 86, 91), (91, 102, 90, 101)]))     # back inside too late
    assert sig is None


def test_fade_requires_stretch_and_day_range():
    sc = {"name": "f", "type": "fade", "side": "short", "level": 200, "target": 150}
    ohlc = [(180, 190, 178, 188), (190, 206, 189, 191)]                   # shooting star at 200
    stretched = dict(ema=150.0, basis=140.0, atr=20.0)
    sig, _ = run(sc, ctx_for(ohlc, day_open=150, day_atr=100, **stretched))
    assert sig["plan"]["ok"] and sig["plan"]["stop"] == 208               # high 206 + 2
    sig, _ = run(sc, ctx_for(ohlc, day_open=170, day_atr=100, **stretched))
    assert not sig["plan"]["ok"] and "day moved 36%" in sig["plan"]["reason"]
    sig, _ = run(sc, ctx_for(ohlc, day_open=150, day_atr=100, ema=185.0, basis=186.0, atr=20.0))
    assert not sig["plan"]["ok"] and "not stretched" in sig["plan"]["reason"]


def test_continuation_pause_break():
    sc = {"name": "c", "type": "continuation_pause", "side": "short", "target": 150}
    ohlc = [(210, 211, 196, 197), (197, 198, 190, 191), (191, 192, 184, 185),   # trending down below 200
            (185, 189, 183, 187), (187, 190, 184, 186),                          # 2-candle pause (183-190)
            (186, 187, 175, 176)]                                                # breaks the pause low
    sig, _ = run(sc, ctx_for(ohlc, ema=200.0, basis=200.0, atr=20.0))
    assert sig["i"] == 5 and sig["plan"]["stop"] == 192 and "2-candle pause" in sig["detail"]


def test_continuation_ema_pullback():
    sc = {"name": "e", "type": "continuation_ema", "side": "long", "target": 160}
    ohlc = [(105, 112, 104, 111), (111, 118, 110, 117), (117, 124, 116, 123),    # trending up above 100
            (123, 124, 99, 112)]                                                 # tags the EMA, closes above
    sig, _ = run(sc, ctx_for(ohlc, ema=[100, 101, 102, 103], basis=95.0, atr=20.0))
    assert sig["i"] == 3 and sig["plan"]["stop"] == 97


def test_session_window_and_expiry():
    sc = {"name": "a", "type": "breakout", "side": "long", "level": 100, "session": "Asia",
          "until": "21:00", "target": 140}
    ohlc = [(97, 100, 96, 99), (98, 99, 96, 97), (97, 99, 96, 98), (99.5, 112, 99, 111)]
    sig, w = run(sc, ctx_for(ohlc, start=ASIA_START))      # breakout candle closes 21:30 ET
    assert sig is None and w.status == "expired"
    sc["until"] = "22:00"
    sig, _ = run(sc, ctx_for(ohlc, start=ASIA_START))
    assert sig is not None
    sig, _ = run({**sc, "session": "NY"}, ctx_for(ohlc, start=ASIA_START))     # wrong session
    assert sig is None


def test_price_condition_and_overrides():
    sc = {"name": "r", "type": "reclaim", "side": "long", "level": 100, "target": 120,
          "conditions": [{"price": "above", "level": 105}]}
    ohlc = [(105, 106, 103, 104), (104, 104, 98, 99), (99, 103, 98.5, 102)]
    sig, _ = run(sc, ctx_for(ohlc))
    assert not sig["plan"]["ok"] and "close not above 105" in sig["plan"]["reason"]
    sig, _ = run({**sc, "conditions": [], "stop": {"beyond": 90}}, ctx_for(ohlc))
    assert sig["plan"]["stop"] == 88                                             # beyond 90 by 2 pts
    sig, _ = run({**sc, "conditions": [], "stop": {"beyond": 0}, "size": 3, "target": 400}, ctx_for(ohlc))
    assert not sig["plan"]["ok"] and "risks" in sig["plan"]["reason"]          # fixed size over $250


def test_default_target_uses_levels_and_once_disarms():
    sc = {"name": "r", "type": "reclaim", "side": "long", "level": "pdVAL"}
    lv = {"pdVAL": 100.0, "pdPOC": 104.0, "pdVAH": 130.0}
    ohlc = [(105, 106, 103, 104), (104, 104, 98, 99), (99, 103, 98.5, 102)]
    sig, w = run(sc, ctx_for(ohlc, levels=lv))
    assert sig["plan"]["target"] == 130                     # pdPOC too close for 1.5R, pdVAH qualifies
    w.took_trade()
    assert not w.armed and w.status == "triggered"


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
