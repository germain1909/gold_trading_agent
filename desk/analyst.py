"""
AI trade analysis: why a journaled trade worked or didn't, and what to take away.

The AI is given only facts from the journal - the scenario's rules, the plan,
the context at trigger time, what the system did, the shadow outcome, the 30m
candles around the trigger, and how similar past triggers went - and is told
not to go beyond them.

Two backends, switchable per request:
    local   LM Studio (OpenAI-compatible API via litellm) - free, private
    claude  Anthropic API, claude-opus-5-5 - deeper analysis; needs
            ANTHROPIC_API_KEY in .env (or an `ant auth login` profile)

    python -m desk.analyst 5            # analyze journal event 5 with the local model
    python -m desk.analyst 5 claude
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Dict, Any, List, Optional

from dotenv import load_dotenv

from . import store
from .scenarios import TYPES

load_dotenv()

CLAUDE_MODEL = os.getenv("ANALYST_CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("ANALYST_CLAUDE_EFFORT", "medium")
LOCAL_MODEL = os.getenv("LM_STUDIO_MODEL", "qwen3-vl-8b-instruct-mlx")
LOCAL_URL = os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1")

SYSTEM_PROMPT = """You review trades for a discretionary NQ futures trader who runs a rules-based scenario \
watcher. Each scenario is a written if-then rule (reclaim, breakout, trap, fade, ...) evaluated on 30-minute \
candles, with a stop, a level target, and 1-3 MNQ of size.

You get one trade's facts as JSON: the scenario rule, the plan (entry, stop, target, R), the context at the \
trigger candle, what the system did (entered, skipped, refused or failed, and why), the shadow outcome \
(what the planned trade did afterwards, whether or not it was taken), the 30m candles around the trigger, \
and how earlier triggers of the same scenario type turned out.

Write a short review with these sections:
1. What happened - two or three sentences.
2. Why it worked or didn't - tie each point to a specific fact (a candle, a level, the stretch, the trend, \
the session, the % of the day's ATR). Separate the idea (was the scenario right?), the timing (was this the \
right candle?), the stop, the target, and execution (did the system act as planned?).
3. Takeaways - one to three. Label each as either "single example" or "pattern (N of M similar triggers)", \
using the history provided. A single trade cannot establish a pattern.

Use only the facts given; if something isn't in them, say it isn't known rather than guessing. No price \
predictions. Keep it under 300 words."""


# ---------- facts ----------

def _candles_around(e: Dict[str, Any], before_h: int = 3, after_h: int = 4) -> List[Dict[str, Any]]:
    """30m candles from a few hours before the trigger to a few hours after."""
    if not e.get("candle_t"):
        return []
    from topstep.client import TopstepClient
    from .backtest import front_month_id
    from .replay import fetch_bars, ET
    t = datetime.fromisoformat(e["candle_t"])
    bars = fetch_bars(TopstepClient(), front_month_id(e["symbol"], date.fromisoformat(e["day"])),
                      t - timedelta(hours=before_h), t + timedelta(hours=after_h))
    return [{"t": datetime.fromisoformat(b["t"]).astimezone(ET).strftime("%m/%d %H:%M"),
             "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
             **({"trigger": True} if b["t"] == e["candle_t"] else {})} for b in bars]


def _history(e: Dict[str, Any]) -> Dict[str, Any]:
    """How earlier triggers of the same scenario type turned out (shadow outcomes)."""
    rows = [x for x in store.events(kinds=["entered", "skipped", "refused", "failed"])
            if x["type"] == e["type"] and x["id"] != e["id"]]
    # the same trigger can appear twice (replay + live rows); never count a trade as its own precedent
    seen, counts, pts = {(e["day"], e["candle_t"], e["scenario"])}, defaultdict(int), []
    for x in rows:
        key = (x["day"], x["candle_t"], x["scenario"])          # one trigger can have replay + live rows
        o = store.outcome(x["id"])
        if key in seen or not o:
            continue
        seen.add(key)
        counts[o["first_hit"]] += 1
        pts.append(o["pnl_pts"])
    return {"scenario_type": e["type"], "earlier_triggers_with_outcomes": len(pts),
            "first_hit_counts": dict(counts), "avg_pnl_pts": round(sum(pts) / len(pts), 1) if pts else None}


def build_facts(event_id: int, with_candles: bool = True) -> Dict[str, Any]:
    e = store.event(event_id)
    if not e:
        raise ValueError(f"no journal event {event_id}")
    return {
        "scenario": {"name": e["scenario"], "type": e["type"], "type_name": TYPES.get(e["type"], e["type"]),
                     "side": e["side"], "rule": e["scenario_json"]},
        "day": e["day"],
        "what_the_system_did": {"event": e["event"], "mode": e["mode"], "source": e["source"],
                                "reason": e["reason"] or None, "trigger_detail": e["detail"]},
        "plan": e["plan"],
        "context_at_trigger": e["context"],
        "result": e["result"],
        "shadow_outcome": store.outcome(event_id),
        "candles_30m_et": _candles_around(e) if with_candles else [],
        "history": _history(e),
    }


# ---------- backends ----------

def _ask_local(facts: Dict[str, Any]) -> str:
    import litellm
    resp = litellm.completion(
        model=f"openai/{LOCAL_MODEL}", api_base=LOCAL_URL, api_key="lm-studio",
        messages=[{"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user", "content": json.dumps(facts, indent=1, default=str)}],
        max_tokens=1200,
    )
    return resp.choices[0].message.content.strip()


def _ask_claude(facts: Dict[str, Any]) -> str:
    import anthropic
    client = anthropic.Anthropic()
    response = client.beta.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=16000,
        output_config={"effort": CLAUDE_EFFORT},
        # If a safety classifier declines the request, re-run it on a fallback model in the same call.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": json.dumps(facts, indent=1, default=str)}],
    )
    if response.stop_reason == "refusal":
        category = response.stop_details.category if response.stop_details else None
        raise RuntimeError(f"Claude declined to analyze this trade (category: {category})")
    return "".join(b.text for b in response.content if b.type == "text").strip()


def analyze(event_id: int, backend: str = "local") -> Dict[str, Any]:
    """Write a review of a journal event and save it. Returns {model, text}."""
    facts = build_facts(event_id)
    if backend == "claude":
        text, model = _ask_claude(facts), CLAUDE_MODEL
    elif backend == "local":
        text, model = _ask_local(facts), f"local:{LOCAL_MODEL}"
    else:
        raise ValueError("backend must be 'local' or 'claude'")
    store.add_analysis(event_id, model, text)
    return {"model": model, "text": text}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    out = analyze(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "local")
    print(f"[{out['model']}]\n{out['text']}")
