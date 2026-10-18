"""
Slack commands for the trading desk. Only the trader's Slack user can use them, and every
change is read back and needs `confirm` before it is written.

    help                       this list
    plan                       today's scenarios: mode and status, open position, day P&L
    add: <idea>                Claude turns the idea into a scenario (LIVE; say "paper" for paper), read back for confirm
    cancel <name>              remove a scenario
    live <name|all>            switch to LIVE (real orders)
    paper <name|all>           switch to paper
    generate [YYYY-MM-DD] [levels...] [notes]
                               Claude drafts a full plan from the market brief (all LIVE)
    use <YYYY-MM-DD>           point the watcher at plans/<date>.json
    kill / resume              kill switch on / off (blocks new live entries)
    flatten                    cancel working orders and close the live position
    confirm / discard          apply or drop the pending change

Changes are written to the plan file; the watcher reloads it within seconds.
"""
import json
import os
import re
import tempfile
from datetime import date
from typing import Callable, Dict, Any, List, Optional

from .scenarios import load, from_dict, describe, ScenarioError
from .store import PROJECT_ROOT

KILL_FILE = os.path.join(PROJECT_ROOT, "data", "KILL")


class SlackCommands:
    def __init__(self, authorized_user: str, get_plan_path: Callable[[], str], set_plan_path: Callable[[str], None],
                 get_runner: Callable[[], Any], say: Callable[[str], Any], trading_day: Callable[[], date],
                 ai=None, broker=None, kill_file: str = KILL_FILE, plans_dir: Optional[str] = None):
        self.user = authorized_user
        self.get_plan_path, self.set_plan_path = get_plan_path, set_plan_path
        self.get_runner, self.say, self.trading_day = get_runner, say, trading_day
        self.ai, self.broker, self.kill_file = ai, broker, kill_file
        self.plans_dir = plans_dir or os.path.join(PROJECT_ROOT, "plans")
        self.pending: Optional[Dict[str, Any]] = None

    # ---------- helpers ----------

    def _levels(self) -> Dict[str, float]:
        r = self.get_runner()
        return dict(r.levels) if r else {}

    def _read(self, path: str) -> List[Dict[str, Any]]:
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return json.load(f)

    def _write(self, path: str, items: List[Dict[str, Any]]) -> None:
        load(json.dumps(items))                                   # validate before touching the file
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(items, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)                                     # atomic: the watcher never sees half a file

    def _find(self, items: List[Dict[str, Any]], name: str) -> Dict[str, Any]:
        n = name.strip().lower()
        exact = [d for d in items if d["name"].lower() == n]
        if exact:
            return exact[0]
        partial = [d for d in items if n in d["name"].lower()]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise ValueError(f"no scenario matches '{name}'. Try `plan` for the names.")
        raise ValueError(f"'{name}' matches several: " + ", ".join(d["name"] for d in partial))

    def _readback(self, d: Dict[str, Any]) -> str:
        return describe(from_dict(dict(d)), self._levels())

    def _propose(self, summary: str, apply: Callable[[], str]) -> str:
        self.pending = {"summary": summary, "apply": apply}
        return f"{summary}\n\nReply `confirm` to apply or `discard` to drop it."

    # ---------- entry point ----------

    def handle(self, user: str, text: str) -> Optional[str]:
        """Handle one message. Returns the reply text (also sent via say), or None if ignored."""
        if user != self.user:
            return None                                           # only the trader can control the desk
        text = (text or "").strip()
        cmd, _, rest = text.partition(" ")
        cmd = cmd.lower().rstrip(":")
        try:
            reply = self._dispatch(cmd, rest.strip(), text)
        except (ValueError, ScenarioError, RuntimeError) as e:
            reply = f":warning: {e}"
        except Exception as e:                                     # never crash the watcher over a command
            reply = f":warning: {type(e).__name__}: {e}"
        if reply:
            self.say(reply)
        return reply

    def _dispatch(self, cmd: str, rest: str, full: str) -> str:
        if cmd in ("help", "?"):
            return "```" + __doc__.split("\n\n", 1)[1].split("\nChanges are", 1)[0] + "```"
        if cmd in ("plan", "status"):
            return self._plan()
        if cmd == "confirm":
            if not self.pending:
                return "Nothing pending."
            apply, self.pending = self.pending["apply"], None
            return apply()
        if cmd in ("discard", "no"):
            had, self.pending = self.pending, None
            return "Dropped the pending change." if had else "Nothing pending."
        if cmd == "add":
            return self._add(full.split(None, 1)[1] if " " in full else "")
        if cmd in ("cancel", "remove"):
            return self._cancel(rest)
        if cmd in ("live", "paper"):
            return self._mode(cmd, rest)
        if cmd == "generate":
            return self._generate(rest)
        if cmd == "use":
            return self._use(rest)
        if cmd == "kill":
            os.makedirs(os.path.dirname(self.kill_file), exist_ok=True)
            open(self.kill_file, "w").close()
            return ":octagonal_sign: Kill switch ON - no new live entries. Open positions keep their Topstep brackets."
        if cmd == "resume":
            if os.path.exists(self.kill_file):
                os.remove(self.kill_file)
            return ":white_check_mark: Kill switch OFF - live entries allowed again."
        if cmd == "flatten":
            if not self.broker or not self.broker.live:
                return "Live trading is off - nothing to flatten."
            return self._propose(":lock: Flatten: cancel working orders and close the live MNQ position at market.",
                                 lambda: (self.broker.flatten("flatten from Slack"), "Flatten sent.")[1])
        return f"Unknown command `{cmd}`. Try `help`."

    # ---------- commands ----------

    def _plan(self) -> str:
        path = self.get_plan_path()
        items = self._read(path)
        r = self.get_runner()
        lines = [f"*Plan* `{os.path.relpath(path, PROJECT_ROOT)}`" + ("" if r else " (not armed yet)")]
        status = {w.sc.name: w.status for w in r.watches} if r else {}
        for d in items:
            lines.append(f"• {d['name']} [{d.get('mode', 'paper').upper()}] {d['type']} {d['side']}"
                         + (f" - {status[d['name']]}" if d["name"] in status else ""))
        if r:
            lines += r.status_lines()[len(r.watches):]
        if os.path.exists(self.kill_file):
            lines.append(":octagonal_sign: kill switch is ON")
        return "\n".join(lines)

    def _add(self, idea: str) -> str:
        if not idea:
            raise ValueError("Usage: `add: if it reclaims pdVAL go long to pdVAH`")
        if not self.ai:
            raise RuntimeError("plan AI isn't available")
        self.say(":hourglass: Turning that into a scenario...")
        d = self.ai.scenario_from_text(idea, self._levels())
        path = self.get_plan_path()
        items = self._read(path)
        if any(x["name"] == d["name"] for x in items):
            d["name"] = f"{d['name']} ({len(items) + 1})"

        def apply():
            self._write(path, self._read(path) + [d])
            tip = f"`paper {d['name']}` to keep it on paper" if d["mode"] == "live" else f"`live {d['name']}` to trade it"
            return f":white_check_mark: Added *{d['name']}* ({d['mode'].upper()}). Use {tip}."
        readback = self._readback(d)                     # validates first: a bad draft is reported, never written
        mode = d.get("mode", "paper")
        icon = ":red_circle:" if mode == "live" else ":large_blue_circle:"
        return self._propose(f"{icon} New scenario ({mode.upper()}):\n```{readback}```", apply)

    def _cancel(self, name: str) -> str:
        path = self.get_plan_path()
        d = self._find(self._read(path), name)

        def apply():
            self._write(path, [x for x in self._read(path) if x["name"] != d["name"]])
            return f":white_check_mark: Removed *{d['name']}*."
        return self._propose(f"Remove *{d['name']}* ({d['type']} {d['side']}, {d.get('mode', 'paper').upper()})?", apply)

    def _mode(self, mode: str, name: str) -> str:
        path = self.get_plan_path()
        items = self._read(path)
        targets = items if name.strip().lower() == "all" else [self._find(items, name)]
        names = [d["name"] for d in targets]
        if not names:
            raise ValueError("the plan has no scenarios")
        if mode == "live" and self.broker is not None and not self.broker.live:
            warn = "\n_Note: LIVE_TRADING isn't `true`, so live scenarios will paper trade (or dry-run)._"
        else:
            warn = ""

        def apply():
            fresh = self._read(path)
            for d in fresh:
                if d["name"] in names:
                    d["mode"] = mode
            self._write(path, fresh)
            return f":white_check_mark: {', '.join(names)} now {mode.upper()}."
        icon = ":red_circle:" if mode == "live" else ":large_blue_circle:"
        return self._propose(f"{icon} Switch to *{mode.upper()}*: {', '.join(names)}"
                             + (" - real orders on your Topstep account" if mode == "live" else "") + warn, apply)

    def _generate(self, rest: str) -> str:
        if not self.ai:
            raise RuntimeError("plan AI isn't available")
        m = re.match(r"(\d{4}-\d{2}-\d{2})\s*(.*)", rest)
        day = date.fromisoformat(m.group(1)) if m else self.trading_day()
        rest = m.group(2) if m else rest
        levels = [float(x.replace(",", "")) for x in re.findall(r"\b\d{4,6}(?:\.\d+)?\b", rest)]
        notes = re.sub(r"\b\d{4,6}(?:\.\d+)?\b", "", rest).strip()
        self.say(f":hourglass: Drafting the {day} plan (market brief + Claude, ~1-2 min)...")
        summary, items = self.ai.draft_plan(day, levels, notes)
        path = os.path.join(self.plans_dir, f"{day.isoformat()}.json")
        exists = os.path.exists(path)

        def apply():
            self._write(path, items)
            modes = sorted({d.get("mode", "paper").upper() for d in items})
            msg = f":white_check_mark: Saved `plans/{day.isoformat()}.json` ({len(items)} scenarios, {'/'.join(modes)})."
            if os.path.abspath(path) != os.path.abspath(self.get_plan_path()):
                msg += (f" The watcher picks it up automatically when the {day} session arms "
                        f"(or `use {day.isoformat()}` to switch now).")
            return msg + " Use `paper <name>` for any you want on paper."
        lines = "\n".join(f"• {d['name']} [{d.get('mode', 'paper').upper()}] - {d['type']} {d['side']} at "
                          f"{d.get('level', '-')}" for d in items)
        return self._propose(f"*Draft plan for {day}*{' (replaces the existing file)' if exists else ''}\n"
                             f"{summary}\n\n*Scenarios:*\n{lines}", apply)

    def _use(self, rest: str) -> str:
        day = date.fromisoformat(rest.strip())
        path = os.path.join(self.plans_dir, f"{day.isoformat()}.json")
        load(open(path).read())                                   # must exist and be valid

        def apply():
            self.set_plan_path(path)
            return f":white_check_mark: Watcher now uses `plans/{day.isoformat()}.json`."
        return self._propose(f"Switch the watcher to `plans/{day.isoformat()}.json`?", apply)
