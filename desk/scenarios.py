"""
Scenarios: the trader's "watch for this" instructions, in the structured
format defined in SCENARIOS.md.

This module loads and validates a scenario (from the trader, the desk agent,
or another AI), resolves level names to prices, and reads it back in plain
English. Nothing is armed until the trader confirms the read-back.
"""
import json
import re
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Union

TYPES = {
    "fade": "A. Fade a stretched level",
    "reclaim": "B. Reclaim and rotate",
    "lose": "B. Lose and rotate",
    "pullback": "C. Hold in a trend",
    "breakout": "D. Breakout and go",
    "retest": "E. Break and retest",
    "trap": "F. Failed breakout",
    "trend_turn": "G. Trend turn",
}
SESSIONS = {"Asia", "London", "NY", "any"}
SESSION_END = {"Asia": "03:00", "London": "09:30", "NY": "15:30", "any": None}
NEEDS_LEVEL = set(TYPES) - {"trend_turn"}
REJECTION_TYPES = {"fade", "pullback", "retest"}

Level = Union[str, float, int]


class ScenarioError(ValueError):
    """A scenario that can't be armed, with a message the trader can act on."""


@dataclass
class Scenario:
    name: str
    type: str
    side: str
    level: Optional[Level] = None
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    session: str = "any"
    until: Optional[str] = None
    target: Level = "default"
    stop: Union[str, Dict[str, Level]] = "default"
    size: Optional[int] = None
    rejection: str = "pattern"
    once: bool = True
    mode: str = "paper"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------- levels ----------

_ATR_RE = re.compile(r"^ATR([+-])(25|50|75|100)%$", re.I)
_DATED_RE = re.compile(r"^(VAH|POC|VAL)[ -](\d{1,2})/(\d{1,2})$", re.I)
_NAMED = {"pdvah": "pdVAH", "vah": "pdVAH", "pdpoc": "pdPOC", "poc": "pdPOC", "pdval": "pdVAL", "val": "pdVAL",
          "open": "open", "the open": "open", "weekly high": "weekly high", "weekly low": "weekly low"}


def normalize_level(ref: Level) -> Level:
    """Canonical form of a level reference, or a ScenarioError if it isn't one."""
    if isinstance(ref, (int, float)) and not isinstance(ref, bool):
        return float(ref)
    if not isinstance(ref, str) or not ref.strip():
        raise ScenarioError(f"'{ref}' is not a level")
    s = ref.strip()
    try:
        return float(s.replace(",", ""))
    except ValueError:
        pass
    if s.lower() in _NAMED:
        return _NAMED[s.lower()]
    m = _ATR_RE.match(s.replace(" ", ""))
    if m:
        return f"ATR{m.group(1)}{m.group(2)}%"
    m = _DATED_RE.match(s)
    if m:
        return f"{m.group(1).upper()}-{int(m.group(2)):02d}/{int(m.group(3)):02d}"
    raise ScenarioError(f"unknown level '{ref}' - use pdVAH/pdPOC/pdVAL, 'VAH 9/22', ATR+50%, open, "
                        f"weekly high/low, or a price")


def resolve_level(ref: Level, levels: Dict[str, float]) -> float:
    """Price for a level reference, given today's named levels (label -> price)."""
    ref = normalize_level(ref)
    if isinstance(ref, float):
        return ref
    if ref in levels:
        return levels[ref]
    raise ScenarioError(f"level '{ref}' isn't available today")


# ---------- loading / validation ----------

def from_dict(d: Dict[str, Any]) -> Scenario:
    if not isinstance(d, dict):
        raise ScenarioError("a scenario must be a JSON object")
    unknown = set(d) - set(Scenario.__dataclass_fields__)
    if unknown:
        raise ScenarioError(f"unknown field(s): {', '.join(sorted(unknown))}")
    for req in ("name", "type", "side"):
        if not d.get(req):
            raise ScenarioError(f"missing '{req}'")
    sc = Scenario(**d)

    if sc.type not in TYPES:
        raise ScenarioError(f"type '{sc.type}' must be one of: {', '.join(TYPES)}")
    if sc.side not in ("long", "short"):
        raise ScenarioError("side must be 'long' or 'short'")
    if sc.type in NEEDS_LEVEL:
        if sc.level is None:
            raise ScenarioError(f"a '{sc.type}' scenario needs a level")
        sc.level = normalize_level(sc.level)
    if sc.type == "reclaim" and sc.side != "long":
        raise ScenarioError("'reclaim' is a long (use 'lose' for the short version)")
    if sc.type == "lose" and sc.side != "short":
        raise ScenarioError("'lose' is a short (use 'reclaim' for the long version)")
    if sc.session not in SESSIONS:
        raise ScenarioError(f"session must be one of: {', '.join(sorted(SESSIONS))}")
    if sc.until is not None and not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", str(sc.until)):
        raise ScenarioError("until must be 'HH:MM' (24h, ET) or null")
    if sc.target != "default":
        sc.target = normalize_level(sc.target)
    if sc.stop != "default":
        if not (isinstance(sc.stop, dict) and set(sc.stop) == {"beyond"}):
            raise ScenarioError("stop must be 'default' or {\"beyond\": <level or price>}")
        sc.stop = {"beyond": normalize_level(sc.stop["beyond"])}
    if sc.size is not None and sc.size not in (1, 2, 3):
        raise ScenarioError("size must be 1, 2, 3 or null")
    if sc.rejection not in ("pattern", "wick"):
        raise ScenarioError("rejection must be 'pattern' or 'wick'")
    if sc.mode not in ("paper", "live"):
        raise ScenarioError("mode must be 'paper' or 'live'")

    cleaned = []
    for c in sc.conditions or []:
        if not isinstance(c, dict) or len(c) == 0:
            raise ScenarioError(f"bad condition {c!r}")
        if "price" in c:
            if c["price"] not in ("above", "below") or "level" not in c or set(c) != {"price", "level"}:
                raise ScenarioError(f"price condition must be {{\"price\": \"above\"/\"below\", \"level\": ...}}: {c!r}")
            cleaned.append({"price": c["price"], "level": normalize_level(c["level"])})
        elif set(c) == {"trend"} and c["trend"] in ("up", "down"):
            cleaned.append(c)
        elif set(c) == {"stretched"} and c["stretched"] is True:
            cleaned.append(c)
        elif set(c) == {"not_choppy"} and c["not_choppy"] is True:
            cleaned.append(c)
        elif set(c) == {"day_range_pct"} and isinstance(c["day_range_pct"], (int, float)) and 0 < c["day_range_pct"] <= 200:
            cleaned.append(c)
        else:
            raise ScenarioError(f"unknown condition {c!r}")
    sc.conditions = cleaned
    return sc


def load(text: str) -> List[Scenario]:
    """One scenario object or a list of them, as JSON text."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ScenarioError(f"not valid JSON: {e.msg} (line {e.lineno})") from e
    return [from_dict(d) for d in (data if isinstance(data, list) else [data])]


# ---------- read-back ----------

def _lv(ref: Level, levels: Optional[Dict[str, float]]) -> str:
    ref = normalize_level(ref)
    if isinstance(ref, float):
        return f"{ref:g}"
    if levels and ref in levels:
        return f"{ref} {levels[ref]:g}"
    return str(ref)


def describe(sc: Scenario, levels: Optional[Dict[str, float]] = None) -> str:
    """Plain-English read-back, in the same terms as SCENARIOS.md."""
    lv = (lambda r: _lv(r, levels))
    up = sc.side == "long"
    lines = [f"Scenario: {sc.name} - {TYPES[sc.type]}, {sc.side.upper()}"]

    when = "any session" if sc.session == "any" else f"{sc.session} only"
    until = sc.until or SESSION_END.get(sc.session) or "end of session"
    lines.append(f"  When: {when}, until {until} ET{', disarms after one trigger' if sc.once else ''}")

    for c in sc.conditions:
        if "price" in c:
            lines.append(f"  Condition: last 30m close {c['price']} {lv(c['level'])}")
        elif "trend" in c:
            lines.append(f"  Condition: trending {c['trend']} (3+ closes {'above' if c['trend'] == 'up' else 'below'} "
                         f"both the EMA line and BB basis)")
        elif "stretched" in c:
            lines.append("  Condition: stretched (at least 1.5x the 30m ATR from the BB basis)")
        elif "not_choppy" in c:
            lines.append("  Condition: not choppy (fewer than 2 line crosses in the last 6 candles)")
        elif "day_range_pct" in c:
            toward = ("up" if not up else "down") if sc.type == "fade" else ("up" if up else "down")
            lines.append(f"  Condition: the day is {toward} at least {c['day_range_pct']:g}% of its ATR from the open")

    L = lv(sc.level) if sc.level is not None else None
    trig = {
        "fade": f"rejects {L} with {'a pattern' if sc.rejection == 'pattern' else 'a pattern or a real wick'}, "
                f"trading back {'up' if up else 'down'}",
        "reclaim": f"trades below {L}, then a 30m candle closes back above it",
        "lose": f"trades above {L}, then a 30m candle closes back below it",
        "pullback": f"holds {L} with the trend ({'pattern' if sc.rejection == 'pattern' else 'pattern or wick'} rejection)",
        "breakout": f"breaks {'above' if up else 'below'} {L}: closes beyond it from the other side with a body "
                    f"of at least half the 30m ATR",
        "retest": f"breaks {'above' if up else 'below'} {L}, then within 6 candles retests it and closes back on "
                  f"the breakout side",
        "trap": f"breaks {'below' if up else 'above'} {L}, then closes back inside within 2 candles",
        "trend_turn": "closes beyond both the EMA line and BB basis after 3+ candles on the other side "
                      "(or with a 1.5 ATR breakout candle)",
    }[sc.type]
    lines.append(f"  Trigger: {trig}")
    lines.append("  Entry: the trigger candle's close")

    if sc.stop != "default":
        stop = f"beyond {lv(sc.stop['beyond'])} (+2 pts)"
    else:
        stop = {
            "fade": "beyond the rejection wick (+2 pts)",
            "pullback": "beyond the rejection wick (+2 pts)",
            "retest": "beyond the retest candle's wick (+2 pts)",
            "reclaim": "below the dip's low (-2 pts)",
            "lose": "above the pop's high (+2 pts)",
            "breakout": "back inside the level past the breakout candle's midpoint (2 pts)",
            "trap": f"beyond the failed break's {'low' if up else 'high'} (2 pts)",
            "trend_turn": "beyond the farther of the two lines (+2 pts)",
        }[sc.type]
    lines.append(f"  Stop: {stop}")
    target = "the first level at least 1.5R away" if sc.target == "default" else lv(sc.target)
    lines.append(f"  Target: {target}")
    size = f"{sc.size} MNQ" if sc.size else "1-3 MNQ"
    lines.append(f"  Size: {size}, max $250 risk, skipped under 1.5R | Mode: {sc.mode.upper()}")
    return "\n".join(lines)
