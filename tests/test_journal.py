"""
Trade journal: events written by the plan runner, and shadow outcomes.

Run: python -m tests.test_journal   (or pytest, if installed)
Uses a temporary database, never data/desk.db.
"""
import os
import tempfile

import desk.store as store
from desk.journal import shadow_outcome


def m(minute, o, h, l, c):
    return {"t": f"2026-10-12T14:{minute:02d}:00+00:00", "o": o, "h": h, "l": l, "c": c}


def test_shadow_outcome_target_stop_and_open():
    short = {"side": "short", "entry": 30960.75, "stop": 30994.5, "target": 30792.0}
    bars = [m(0, 30960, 30974.5, 30940, 30950), m(1, 30950, 30955, 30867.25, 30894.5)]
    o = shadow_outcome(short, bars)
    assert o["first_hit"] == "open" and o["mfe_pts"] == 93.5 and o["mae_pts"] == 13.75
    assert o["pnl_pts"] == 66.25 and o["pnl_usd_1"] == 132.5
    o = shadow_outcome(short, bars + [m(2, 30894, 30900, 30780, 30790)])
    assert o["first_hit"] == "target" and o["exit_price"] == 30792.0 and o["minutes"] == 2
    o = shadow_outcome(short, [m(0, 30960, 31000, 30780, 30800)])      # both in one bar: stop wins
    assert o["first_hit"] == "stop" and o["pnl_usd_1"] == -67.5


def test_runner_writes_the_journal():
    from tests.test_plan_runner import make, RECLAIM
    with tempfile.TemporaryDirectory() as tmp:
        old = store.DB_PATH
        store.DB_PATH = os.path.join(tmp, "j.db")
        try:
            runner, ctx, _ = make([RECLAIM, {**RECLAIM, "name": "blocked", "conditions": [{"price": "above", "level": 105}]},
                                   {"name": "needs ATR", "type": "fade", "side": "short", "level": "ATR+50%"}],
                                  journal=True)
            for i in range(len(ctx.candles)):
                runner.on_candle(ctx, i)
            runner.on_price(120.25)
            evs = store.events("2026-10-12")
            kinds = [e["event"] for e in evs]
            assert kinds.count("invalidated") == 1 and "skipped" in kinds and "entered" in kinds and "closed" in kinds
            entered = next(e for e in evs if e["event"] == "entered")
            assert entered["plan"]["entry"] == 102 and entered["context"]["session"] == "NY"
            assert entered["context"]["position_vs_lines"] in ("above both", "below both", "between")
            assert entered["result"]["exit"]["how"] == "target"                 # written back on the entry row
            skipped = next(e for e in evs if e["event"] == "skipped")
            assert "close not above 105" in skipped["reason"]
        finally:
            store.DB_PATH = old


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
