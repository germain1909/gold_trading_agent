"""
Watcher scheduling: the evening auto-plan runs once, Sun-Thu, between AUTO_PLAN_TIME and the 18:00 open.

Run: python -m tests.test_watcher   (or pytest, if installed)
"""
import threading
from datetime import datetime, date

import desk.watcher as W
from desk.replay import ET


class FakeCommands:
    def __init__(self):
        self.days = []

    def auto_generate(self, day):
        self.days.append(day)


def at(y, mo, d, h, mi):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            t = datetime(y, mo, d, h, mi, tzinfo=ET)
            return t.astimezone(tz) if tz else t
    return Clock


def watcher():
    w = W.LevelWatcher.__new__(W.LevelWatcher)
    w.commands, w.auto_plan_day = FakeCommands(), None
    return w


def run_at(w, when):
    old = W.datetime
    W.datetime = when
    try:
        w.check_auto_plan()
    finally:
        W.datetime = old
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.daemon:
            t.join(1)


def test_auto_plan_runs_once_in_the_evening_window():
    w = watcher()
    run_at(w, at(2026, 10, 19, 17, 10))                   # Mon 17:10: too early
    run_at(w, at(2026, 10, 19, 17, 15))                   # Mon 17:15: plans Tuesday
    run_at(w, at(2026, 10, 19, 17, 40))                   # same evening: not again
    assert w.commands.days == [date(2026, 10, 20)]
    run_at(w, at(2026, 10, 19, 18, 5))                    # after the open: never
    assert w.commands.days == [date(2026, 10, 20)]


def test_auto_plan_skips_friday_and_saturday_and_plans_monday_on_sunday():
    w = watcher()
    run_at(w, at(2026, 10, 23, 17, 20))                   # Friday evening: no session tonight
    run_at(w, at(2026, 10, 24, 17, 20))                   # Saturday
    assert w.commands.days == []
    run_at(w, at(2026, 10, 25, 17, 20))                   # Sunday: Monday's plan
    assert w.commands.days == [date(2026, 10, 26)]


def test_auto_plan_can_be_turned_off():
    old = W.AUTO_PLAN_TIME
    W.AUTO_PLAN_TIME = "off"
    try:
        w = watcher()
        run_at(w, at(2026, 10, 19, 17, 30))
        assert w.commands.days == []
    finally:
        W.AUTO_PLAN_TIME = old


if __name__ == "__main__":
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print(f"ok  {name}")
    print(f"{len(tests)} passed")
