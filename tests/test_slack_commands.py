"""
Slack commands: authorization, confirm flow, plan edits, generate, kill switch - and live plan reload.

Run: python -m tests.test_slack_commands   (or pytest, if installed)
No Slack, no Claude, no Topstep: fakes stand in for all three.
"""
import json
import os
import tempfile
from datetime import date

from desk.slack_commands import SlackCommands
from desk.scenarios import from_dict
from tests.test_plan_runner import make, RECLAIM

ME, OTHER = "U_TRADER", "U_SOMEONE"
PLAN = [
    {"name": "Reclaim pdVAL", "type": "reclaim", "side": "long", "level": "pdVAL", "target": "pdVAH", "mode": "paper"},
    {"name": "Fade Friday VAH", "type": "fade", "side": "short", "level": 31128, "target": "pdPOC", "mode": "live"},
]


class FakeAI:
    def scenario_from_text(self, text, levels):
        return {"name": "Break above pdVAH", "type": "breakout", "side": "long", "level": "pdVAH", "mode": "paper"}

    def draft_plan(self, day, levels, notes):
        self.args = (day, levels, notes)
        return "Bias neutral.", [dict(PLAN[0], mode="paper")]


class FakeBroker:
    live = True

    def __init__(self):
        self.flattened = False

    def flatten(self, reason):
        self.flattened = True


def setup(tmp):
    plans = os.path.join(tmp, "plans")
    os.makedirs(plans)
    path = os.path.join(plans, "2026-10-14.json")
    with open(path, "w") as f:
        json.dump(PLAN, f)
    said, state = [], {"path": path}
    cmds = SlackCommands(ME, lambda: state["path"], lambda p: state.update(path=p), lambda: None, said.append,
                         lambda: date(2026, 10, 15), ai=FakeAI(), broker=FakeBroker(),
                         kill_file=os.path.join(tmp, "KILL"), plans_dir=plans)
    return cmds, said, state


def read(path):
    with open(path) as f:
        return json.load(f)


def test_only_the_trader_is_heard():
    with tempfile.TemporaryDirectory() as tmp:
        cmds, said, _ = setup(tmp)
        assert cmds.handle(OTHER, "kill") is None and said == []
        assert not os.path.exists(os.path.join(tmp, "KILL"))


def test_live_switch_needs_confirm():
    with tempfile.TemporaryDirectory() as tmp:
        cmds, said, state = setup(tmp)
        reply = cmds.handle(ME, "live reclaim")
        assert "Switch to *LIVE*: Reclaim pdVAL" in reply and "real orders" in reply
        assert read(state["path"])[0]["mode"] == "paper"                     # nothing written yet
        cmds.handle(ME, "confirm")
        assert read(state["path"])[0]["mode"] == "live"
        cmds.handle(ME, "paper all")
        cmds.handle(ME, "discard")
        assert read(state["path"])[1]["mode"] == "live"                      # discarded: unchanged


def test_add_and_cancel():
    with tempfile.TemporaryDirectory() as tmp:
        cmds, said, state = setup(tmp)
        reply = cmds.handle(ME, "add: if it breaks above pdVAH go long")
        assert "New scenario (PAPER)" in reply and "Breakout and go" in reply
        cmds.handle(ME, "confirm")
        assert [d["name"] for d in read(state["path"])][-1] == "Break above pdVAH"
        cmds.handle(ME, "cancel fade")
        cmds.handle(ME, "confirm")
        assert "Fade Friday VAH" not in [d["name"] for d in read(state["path"])]


def test_ambiguous_or_unknown_names():
    with tempfile.TemporaryDirectory() as tmp:
        cmds, _, _ = setup(tmp)
        assert "no scenario matches" in cmds.handle(ME, "cancel nothing-like-this")
        assert "Unknown command" in cmds.handle(ME, "dance")


def test_generate_saves_next_days_plan_on_confirm():
    with tempfile.TemporaryDirectory() as tmp:
        cmds, said, _ = setup(tmp)
        reply = cmds.handle(ME, "generate 31370 30792 watch the weekly low")
        assert "Draft plan for 2026-10-15" in reply and "Bias neutral." in reply
        assert cmds.ai.args == (date(2026, 10, 15), [31370.0, 30792.0], "watch the weekly low")
        out = os.path.join(tmp, "plans", "2026-10-15.json")
        assert not os.path.exists(out)
        drawn = []
        cmds.chart = lambda day, path: (drawn.append((day, path)), "Posted.")[1]
        assert cmds.handle(ME, "confirm") == "Posted."                         # saved, then the PDF goes out
        assert read(out)[0]["mode"] == "paper" and drawn == [(date(2026, 10, 15), out)]
        assert "Saved `plans/2026-10-15.json`" in said[-2]
        cmds.chart = lambda day, path: 1 / 0
        cmds.handle(ME, "generate")
        assert "Plan saved, but the PDF failed" in cmds.handle(ME, "confirm")    # a PDF problem never loses the plan


def test_use_kill_resume_flatten():
    with tempfile.TemporaryDirectory() as tmp:
        cmds, _, state = setup(tmp)
        other = os.path.join(tmp, "plans", "2026-10-15.json")
        with open(other, "w") as f:
            json.dump(PLAN, f)
        cmds.handle(ME, "use 2026-10-15")
        cmds.handle(ME, "confirm")
        assert state["path"] == other
        cmds.handle(ME, "kill")
        assert os.path.exists(os.path.join(tmp, "KILL"))
        cmds.handle(ME, "resume")
        assert not os.path.exists(os.path.join(tmp, "KILL"))
        cmds.handle(ME, "flatten")
        assert not cmds.broker.flattened
        cmds.handle(ME, "confirm")
        assert cmds.broker.flattened


def test_chart_posts_the_days_plan():
    with tempfile.TemporaryDirectory() as tmp:
        cmds, said, _ = setup(tmp)
        drawn = []
        cmds.chart = lambda day, path: (drawn.append((day, os.path.basename(path))), "Posted.")[1]
        assert "no plan for 2026-10-15" in cmds.handle(ME, "chart")            # trading day 10/15 has no file
        assert cmds.handle(ME, "chart 2026-10-14") == "Posted."
        assert drawn == [(date(2026, 10, 14), "2026-10-14.json")] and "Drawing" in said[-2]


def test_auto_plan_saves_posts_and_never_overwrites():
    with tempfile.TemporaryDirectory() as tmp:
        cmds, said, _ = setup(tmp)
        drawn = []
        cmds.chart = lambda day, path: (drawn.append(day), "Posted.")[1]
        cmds.auto_generate(date(2026, 10, 14))                               # the trader's own plan exists
        assert "Auto-plan skipped" in said[-1] and drawn == []
        cmds.auto_generate(date(2026, 10, 15))
        out = os.path.join(tmp, "plans", "2026-10-15.json")
        assert read(out)[0]["name"] == "Reclaim pdVAL" and drawn == [date(2026, 10, 15)]
        assert any("Auto plan for Thu 10/15" in m and "Bias neutral." in m for m in said) and said[-1] == "Posted."
        cmds.ai.draft_plan = lambda day, levels, notes: 1 / 0
        cmds.auto_generate(date(2026, 10, 16))
        assert "Auto-plan failed" in said[-1] and "keeps using `2026-10-14.json`" in said[-1]
        assert not os.path.exists(os.path.join(tmp, "plans", "2026-10-16.json"))


def test_invalid_edit_never_reaches_the_file():
    with tempfile.TemporaryDirectory() as tmp:
        cmds, _, state = setup(tmp)
        before = read(state["path"])
        cmds.ai.scenario_from_text = lambda text, levels: {"name": "bad", "type": "reclaim", "side": "short",
                                                           "level": "pdVAL"}
        reply = cmds.handle(ME, "add: something odd")
        assert "'reclaim' is a long" in reply and read(state["path"]) == before and cmds.pending is None


def test_drafts_default_to_live_but_respect_paper():
    import desk.plan_ai as ai
    d = {"name": "x", "type": "reclaim", "side": "long", "level": "pdVAL", "mode": "paper"}
    assert ai.DEFAULT_MODE == "live" and ai._validate([d])[0]["mode"] == "live"    # Claude's mode is overridden
    assert ai._validate([d], mode="paper")[0]["mode"] == "paper"
    calls = {}
    old = ai._ask_validated
    ai._ask_validated = lambda system, prompt, single, mode="live": (calls.setdefault("mode", mode), ("", [d]))[1]
    try:
        ai.scenario_from_text("keep this one on paper: reclaim pdVAL")
        assert calls["mode"] == "paper"
        calls.clear()
        ai.scenario_from_text("reclaim pdVAL long")
        assert calls["mode"] == "live"
    finally:
        ai._ask_validated = old


def test_reload_keeps_unchanged_scenarios_and_the_position():
    runner, ctx, _ = make([RECLAIM, {**RECLAIM, "name": "other"}])
    for i in range(len(ctx.candles)):
        runner.on_candle(ctx, i)
    assert runner.position is not None
    kept = runner.watches[1]
    changes = runner.reload([from_dict(dict(RECLAIM, mode="live")), from_dict({**RECLAIM, "name": "other"}),
                             from_dict({**RECLAIM, "name": "new one"})], ctx, list(range(len(ctx.candles))))
    assert runner.watches[1] is kept                                   # unchanged: same object, same state
    assert not runner.watches[0].armed                                 # traded + once: an edit doesn't re-arm it
    assert any(c.startswith("changed") for c in changes) and any(c.startswith("added") for c in changes)
    assert runner.position is not None                                 # the open position is untouched
    runner.reload([from_dict(RECLAIM)], ctx, [])
    assert [w.sc.name for w in runner.watches] == ["reclaim VAL"]


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
