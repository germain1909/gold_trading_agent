"""
Plan runner: entries, exits, skips, expiry, daily limit - and what it would send to Slack.

Run: python -m tests.test_plan_runner   (or pytest, if installed)
"""
from datetime import date

from desk.plan_runner import PlanRunner
from desk.risk import RiskConfig
from desk.scenarios import from_dict
from tests.test_detector import ctx_for, ASIA_START

CFG = RiskConfig(point_value=2.0, min_contracts=1, max_contracts=3, max_risk_per_trade=250,
                 stop_buffer_pts=2.0, min_rr=1.5, min_target_pts=10.0, daily_loss_limit=100)


class FakeSlack:
    def __init__(self):
        self.sent = []

    def __getattr__(self, name):
        if name.startswith("send_"):
            return lambda *a, **k: self.sent.append((name, a))
        raise AttributeError(name)


RECLAIM = {"name": "reclaim VAL", "type": "reclaim", "side": "long", "level": "pdVAL", "target": 120}
OHLC = [(105, 106, 103, 104), (104, 104, 98, 99), (99, 103, 98.5, 102)]


def make(scenarios, ohlc=OHLC, levels=None, **ctx_kw):
    slack = FakeSlack()
    lv = levels or {"pdVAL": 100.0}
    ctx = ctx_for(ohlc, levels=lv, **ctx_kw)
    runner = PlanRunner([from_dict(s) for s in scenarios], lv, 100.0, 400.0, "NQ", "Z26",
                        date(2026, 10, 12), slack, CFG, quiet=True)
    return runner, ctx, slack


def kinds(slack):
    return [k for k, _ in slack.sent]


def test_entry_then_target():
    runner, ctx, slack = make([RECLAIM])
    for i in range(len(ctx.candles)):
        runner.on_candle(ctx, i)
    assert runner.position and runner.position["plan"]["entry"] == 102
    runner.on_price(110)
    runner.on_price(120.25)
    assert runner.position is None and runner.closed[0]["pnl"] == (120 - 102) * 2 * 3
    runner.end_of_day()
    assert kinds(slack) == ["send_session_started", "send_trade_entered", "send_trade_closed", "send_daily_summary"]


def test_stop_and_daily_limit():
    runner, ctx, slack = make([RECLAIM])
    for i in range(len(ctx.candles)):
        runner.on_candle(ctx, i)
    runner.on_price(95.75)                       # stop is 96: loss 6 pts x 3 x $2 = $36 ... below the $100 limit
    assert runner.position is None and not runner.limit_hit
    runner.day_pnl = -90
    runner.position = {"scenario": "x", "mode": "paper", "entry_time": None,
                       "plan": {"side": "long", "entry": 100, "stop": 96, "target": 120, "contracts": 3}}
    runner.on_price(96)
    assert runner.limit_hit and "send_daily_limit_hit" in kinds(slack)


def test_trigger_blocked_is_reported():
    runner, ctx, slack = make([{**RECLAIM, "conditions": [{"price": "above", "level": 105}]}])
    for i in range(len(ctx.candles)):
        runner.on_candle(ctx, i)
    assert runner.position is None
    name, args = slack.sent[-1]
    assert name == "send_scenario_skipped" and "close not above 105" in args[-1]


def test_missing_level_and_expiry_are_invalidations():
    expiring = {"name": "asia break", "type": "breakout", "side": "long", "level": 100, "session": "Asia",
                "until": "21:00", "target": 140}
    missing = {"name": "needs ATR", "type": "fade", "side": "short", "level": "ATR+50%"}
    ohlc = [(97, 100, 96, 99), (98, 99, 96, 97), (97, 99, 96, 98), (99.5, 112, 99, 111)]
    runner, ctx, slack = make([expiring, missing], ohlc=ohlc, start=ASIA_START)
    for i in range(len(ctx.candles)):
        runner.on_candle(ctx, i)
    invalid = [a for k, a in slack.sent if k == "send_scenario_invalidated"]
    assert any("needs ATR" in a[1] and "not armed" in a[2] for a in invalid)
    assert any("asia break" in a[1] and "expired" in a[2] for a in invalid)


def test_warmup_updates_memory_without_trading():
    runner, ctx, slack = make([RECLAIM])
    runner.on_candle(ctx, 0, warmup=True)
    runner.on_candle(ctx, 1, warmup=True)        # the dip is remembered...
    runner.on_candle(ctx, 2)                     # ...so the live reclaim candle triggers
    assert runner.position is not None and kinds(slack) == ["send_session_started", "send_trade_entered"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
