"""
Claude drafts scenarios and plans for the trading desk.

    scenario_from_text("if it reclaims pdVAL go long to pdVAH", levels)  -> one scenario dict
    draft_plan(date(2026, 10, 14), [31370], "watch the weekly low")       -> (summary text, [scenario dicts])

Claude gets the playbook (SCENARIOS.md) as its rules, and every scenario it returns
must pass the desk's own validator (desk.scenarios). If it doesn't, Claude gets the
validator's error back and one more try. Drafts are always "paper"; going live is the
trader's decision.

Needs ANTHROPIC_API_KEY in .env (or an `ant auth login` profile).
"""
import json
import os
import re
import subprocess
import sys
from datetime import date
from typing import Dict, Any, List, Tuple, Optional

from dotenv import load_dotenv

from .scenarios import from_dict, ScenarioError
from .store import PROJECT_ROOT

load_dotenv()

MODEL = os.getenv("PLAN_AI_MODEL", os.getenv("ANALYST_CLAUDE_MODEL", "claude-opus-5-5"))
EFFORT = os.getenv("PLAN_AI_EFFORT", "medium")

EVIDENCE = """Backtest evidence so far (30-minute candles, 1-3 MNQ, Aug-Oct 2026):
- Breakouts of the prior day's VAH/VAL were the strongest generic scenarios (37 trades, ~46% win).
- Traps (failed breakouts) and fades did well in small samples.
- "lose pdVAH" was weak (25 trades, 20% win) - avoid unless asked.
- 30m candles beat 15m/10m/5m overall. The breakout stop rule (level + 2 or the candle midpoint) beat wider stops."""


def _playbook() -> str:
    with open(os.path.join(PROJECT_ROOT, "SCENARIOS.md")) as f:
        return f.read()


def _system(task: str) -> str:
    return (f"You write scenarios for an NQ futures trading desk. The rules below are the only scenario "
            f"language the desk understands; use exactly these types, fields, level words and conditions.\n\n"
            f"{_playbook()}\n\n{EVIDENCE}\n\n{task}")


def _ask(system: str, messages: List[Dict[str, Any]]) -> str:
    import anthropic
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": EFFORT},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        cache_control={"type": "ephemeral"},          # the playbook prefix is identical every call
        system=system,
        messages=messages,
    )
    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise RuntimeError(f"Claude declined the request (category: {category})")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def _json_block(text: str) -> Any:
    m = re.search(r"```json\s*(.*?)```", text, re.S)
    if not m:
        raise ValueError("no ```json block in the reply")
    return json.loads(m.group(1))


def _validate(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    names = set()
    for d in items:
        d = {**d, "mode": "paper"}                       # drafts are never live
        from_dict(dict(d))                               # raises ScenarioError with a usable message
        if d["name"] in names:
            raise ScenarioError(f"two scenarios are named '{d['name']}' - names must be unique")
        names.add(d["name"])
        out.append(d)
    return out


def _ask_validated(system: str, prompt: str, single: bool) -> Tuple[str, List[Dict[str, Any]]]:
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        text = _ask(system, messages)
        try:
            data = _json_block(text)
            items = [data] if single and isinstance(data, dict) else data
            if not isinstance(items, list):
                raise ValueError("the JSON must be " + ("one object" if single else "an array"))
            return text, _validate(items)
        except (ValueError, ScenarioError, json.JSONDecodeError) as e:
            if attempt == 1:
                raise RuntimeError(f"Claude's draft didn't pass the desk's validator: {e}")
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"The desk rejected that: {e}. Fix it and send the "
                                                     f"corrected ```json block."}]
    raise RuntimeError("unreachable")


def scenario_from_text(text: str, levels: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Turn one plain-English idea into one validated scenario (paper)."""
    lv = "\n".join(f"  {k}: {v:.2f}" for k, v in sorted((levels or {}).items(), key=lambda kv: -kv[1])) or "  (unknown)"
    system = _system("Convert the trader's one idea into exactly one scenario. Reply with one short sentence "
                     "restating the scenario in playbook words, then a ```json block containing ONE scenario "
                     "object. If the idea doesn't fit any scenario type, say so instead and give no JSON.")
    _, items = _ask_validated(system, f"Today's levels:\n{lv}\n\nThe trader's idea: {text}", single=True)
    return items[0]


def market_brief(day: date, extra: List[float]) -> str:
    out = subprocess.run([sys.executable, "-m", "desk.market_brief", day.isoformat(), *[str(x) for x in extra]],
                         capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=600)
    text = re.sub(r"\x1b\[[0-9;]*m", "", out.stdout)
    return "\n".join(ln for ln in text.splitlines() if not ln.startswith("Topstep:"))


def draft_plan(day: date, extra_levels: List[float], notes: str = "") -> Tuple[str, List[Dict[str, Any]]]:
    """Draft a full plan for `day` from the market brief. Returns (plain-English summary, scenarios)."""
    brief = market_brief(day, extra_levels)
    system = _system(
        "Draft the day's plan from the market brief. First a plain-English summary for Slack (under 250 "
        "words): one line per timeframe, the key levels with confluences, the bias and what changes it, "
        "then one if-then line per scenario with its type and evidence, and the no-trade conditions. Then a "
        "```json block with the array of 4-7 scenarios. Use pdVAH/pdPOC/pdVAL for the prior day's value "
        "area and plain prices or dated levels (e.g. 'VAH 10/9') for others. Prefer levels with confluence, "
        "cover both directions where the levels support it, and flag any of the trader's ideas that "
        "conflict with the read.")
    prompt = f"Plan for {day.isoformat()}.\n\nTrader's notes: {notes or '(none)'}\n\nMarket brief:\n{brief}"
    text, items = _ask_validated(system, prompt, single=False)
    summary = re.sub(r"```json.*?```", "", text, flags=re.S).strip()
    return summary, items
