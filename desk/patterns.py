"""
Candlestick analysis of a closed candle against a price level.

Pure functions, no I/O, so they're easy to test and reuse in backtests.
A candle is a dict with o, h, l, c (and optionally v, start).
"""
from typing import Dict, Any, List, Optional

# Pattern thresholds (fractions of the candle's range unless noted)
WICK_TO_BODY = 2.0      # rejection wick must be at least 2x the body
WICK_MIN_RANGE = 0.5    # ...and at least half the candle's range
OPPOSITE_WICK_MAX = 0.25
DOJI_BODY_MAX = 0.1
REJECTION_WICK_MIN = 0.3  # a "wick" rejection needs the wick toward the level to be >= 30% of range

# Rejection strength grades, weakest first
STRENGTHS = ["weak", "wick", "pattern"]


def _parts(c: Dict[str, Any]):
    rng = c["h"] - c["l"]
    body = abs(c["c"] - c["o"])
    upper = c["h"] - max(c["o"], c["c"])
    lower = min(c["o"], c["c"]) - c["l"]
    return rng, body, upper, lower


def is_shooting_star(c: Dict[str, Any]) -> bool:
    rng, body, upper, lower = _parts(c)
    return rng > 0 and upper >= WICK_TO_BODY * body and upper >= WICK_MIN_RANGE * rng and lower <= OPPOSITE_WICK_MAX * rng


def is_hammer(c: Dict[str, Any]) -> bool:
    rng, body, upper, lower = _parts(c)
    return rng > 0 and lower >= WICK_TO_BODY * body and lower >= WICK_MIN_RANGE * rng and upper <= OPPOSITE_WICK_MAX * rng


def is_doji(c: Dict[str, Any]) -> bool:
    rng, body, _, _ = _parts(c)
    return rng > 0 and body <= DOJI_BODY_MAX * rng


def is_bearish_engulfing(c: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> bool:
    if not prev:
        return False
    return prev["c"] > prev["o"] and c["c"] < c["o"] and c["o"] >= prev["c"] and c["c"] <= prev["o"]


def is_bullish_engulfing(c: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> bool:
    if not prev:
        return False
    return prev["c"] < prev["o"] and c["c"] > c["o"] and c["o"] <= prev["c"] and c["c"] >= prev["o"]


def bearish_patterns(c: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> List[str]:
    found = []
    if is_shooting_star(c):
        found.append("shooting star")
    if is_bearish_engulfing(c, prev):
        found.append("bearish engulfing")
    if is_doji(c):
        found.append("doji")
    return found


def bullish_patterns(c: Dict[str, Any], prev: Optional[Dict[str, Any]]) -> List[str]:
    found = []
    if is_hammer(c):
        found.append("hammer")
    if is_bullish_engulfing(c, prev):
        found.append("bullish engulfing")
    if is_doji(c):
        found.append("doji")
    return found


def rejection_strength(c: Dict[str, Any], role: str, patterns: List[str]) -> str:
    """
    Grade a rejection:
      "pattern" - a named reversal pattern formed at the level
      "wick"    - no pattern, but the wick toward the level is a big part of the candle
      "weak"    - closed back across the level, but the level barely pushed price back
                  (e.g. a momentum candle that just happened to open next to it)
    """
    if any(p != "doji" for p in patterns):
        return "pattern"
    rng, _, upper, lower = _parts(c)
    wick = upper if role == "resistance" else lower
    if rng > 0 and wick >= REJECTION_WICK_MIN * rng:
        return "wick"
    return "weak"


def evaluate_level(level_price: float, c: Dict[str, Any], prev: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Decide what a closed candle did at a level.

    The side the candle opened on tells us whether the level was tested as
    resistance (opened below, traded up to it) or support (opened above,
    traded down to it). The close decides the outcome:
      - closed back on the opening side -> "rejected"
      - closed beyond the level         -> "accepted"

    Rejections also get a "strength" grade (see rejection_strength).

    Returns None if the candle never reached the level, opened exactly on it,
    or closed exactly on it.
    """
    L = level_price
    if c["o"] < L and c["h"] >= L:
        role = "resistance"
        if c["c"] < L:
            pats = bearish_patterns(c, prev)
            return {"role": role, "outcome": "rejected", "bias": "bearish", "patterns": pats,
                    "strength": rejection_strength(c, role, pats)}
        if c["c"] > L:
            return {"role": role, "outcome": "accepted", "bias": "bullish", "patterns": bullish_patterns(c, prev),
                    "strength": ""}
    elif c["o"] > L and c["l"] <= L:
        role = "support"
        if c["c"] > L:
            pats = bullish_patterns(c, prev)
            return {"role": role, "outcome": "rejected", "bias": "bullish", "patterns": pats,
                    "strength": rejection_strength(c, role, pats)}
        if c["c"] < L:
            return {"role": role, "outcome": "accepted", "bias": "bearish", "patterns": bearish_patterns(c, prev),
                    "strength": ""}
    return None
