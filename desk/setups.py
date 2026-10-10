"""
The two trade setups, as pure functions over closed 30m candles.

Setup 1 - TREND CROSS (ride it)
    A candle closes beyond BOTH the EMA line (EMA 15 smoothed SMA 5) and the
    BB basis (EMA 24) after the previous candle was not beyond both, AND either
    price was on the other side of both lines for CROSS_MIN_PRIOR candles first
    or the cross candle is a breakout (body >= CROSS_BREAKOUT_BODY_ATR x ATR).
    Entry at that close, stop beyond the farther line + buffer, target the first
    level at least MIN_RR away; exit early if a candle closes back across the
    EMA line.

Setup 2 - OVERSTRETCHED REJECTION
    A [pattern] rejection at one of the trader's levels while price is stretched
    away from the averages, trading back toward them: short when the close is
    above both lines by at least STRETCH_MIN x ATR from the BB basis, long when
    below. Entry at the rejection close, stop beyond the wick, target the first
    level at least MIN_RR away.

Each signal: {"setup", "i" (candle index), "t", "entry_time", "side", "plan", ...}
"""
import os
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

from .indicators import crossed_for_entry
from .patterns import evaluate_level
from .risk import RiskConfig, plan_trade, size_position, round_to_tick

STRETCH_MIN = float(os.getenv("SETUP_STRETCH_ATR", 1.5))
# Trend-cross filters - a cross needs a reason, not a wiggle through flat lines.
# It qualifies if EITHER:
#   CROSS_MIN_PRIOR         - price closed on the other side of both lines for at
#                             least this many candles in a row first (a real trend change)
#   CROSS_BREAKOUT_BODY_ATR - or the cross candle's body is at least this many ATRs
#                             (a decisive breakout out of chop)
# CROSS_MIN_BODY_ATR is an extra, optional minimum body for every cross (0 = off).
CROSS_MIN_PRIOR = int(os.getenv("SETUP_CROSS_MIN_PRIOR", 3))
CROSS_BREAKOUT_BODY_ATR = float(os.getenv("SETUP_CROSS_BREAKOUT_BODY_ATR", 1.5))
CROSS_MIN_BODY_ATR = float(os.getenv("SETUP_CROSS_MIN_BODY_ATR", 0))


def _entry_time(candle: Dict[str, Any], minutes: int) -> datetime:
    return datetime.fromisoformat(candle["t"]) + timedelta(minutes=minutes)


def trend_cross_signals(
    candles: List[Dict[str, Any]],
    ema_l: List[Optional[float]],
    basis: List[Optional[float]],
    minutes: int,
    levels: Optional[List[Dict[str, Any]]] = None,
    cfg: Optional[RiskConfig] = None,
    atr_v: Optional[List[Optional[float]]] = None,
    min_prior: int = CROSS_MIN_PRIOR,
    min_body_atr: float = CROSS_MIN_BODY_ATR,
    breakout_body_atr: float = CROSS_BREAKOUT_BODY_ATR,
) -> List[Dict[str, Any]]:
    """
    Every fresh cross, each marked "qualified" or with a "why_not" from the filters.
    """
    cfg = cfg or RiskConfig()
    level_prices = [lv["price"] for lv in levels or []]
    out = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        for side in ("long", "short"):
            now = crossed_for_entry(side, c["c"], ema_l[i], basis[i])
            before = crossed_for_entry(side, p["c"], ema_l[i - 1], basis[i - 1])
            if not now or before:
                continue
            other = "short" if side == "long" else "long"
            prior = 0
            for k in range(i - 1, -1, -1):
                if not crossed_for_entry(other, candles[k]["c"], ema_l[k], basis[k]):
                    break
                prior += 1
            a = atr_v[i] if atr_v else None
            body_atr = abs(c["c"] - c["o"]) / a if a else None
            why_not = None
            breakout = bool(breakout_body_atr) and body_atr is not None and body_atr >= breakout_body_atr
            if prior < min_prior and not breakout:
                why_not = f"only {prior} candle(s) on the other side first (< {min_prior})"
            elif min_body_atr and (body_atr is None or body_atr < min_body_atr):
                why_not = f"cross body {body_atr or 0:.2f} ATR < {min_body_atr}"
            if side == "long":
                stop = round_to_tick(min(ema_l[i], basis[i]) - cfg.stop_buffer_pts, cfg.tick_size, up=False)
            else:
                stop = round_to_tick(max(ema_l[i], basis[i]) + cfg.stop_buffer_pts, cfg.tick_size, up=True)
            out.append({
                "setup": "trend cross",
                "i": i,
                "t": c["t"],
                "entry_time": _entry_time(c, minutes),
                "side": side,
                "detail": f"closed {'above' if side == 'long' else 'below'} EMA {ema_l[i]:.2f} / BB {basis[i]:.2f}, "
                          f"{prior} prior, body {abs(c['c'] - c['o']):g} pts"
                          f"{f' = {body_atr:.2f} ATR' if body_atr is not None else ''}",
                "qualified": why_not is None,
                "why_not": why_not,
                "plan": size_position(side, c["c"], stop, level_prices, cfg),
            })
    return out


def stretched_rejection_signals(
    candles: List[Dict[str, Any]],
    ema_l: List[Optional[float]],
    basis: List[Optional[float]],
    atr_v: List[Optional[float]],
    levels: List[Dict[str, Any]],
    minutes: int,
    cfg: Optional[RiskConfig] = None,
    stretch_min: float = STRETCH_MIN,
) -> List[Dict[str, Any]]:
    """
    Returns every [pattern] rejection, each marked "qualified" (stretched enough,
    in the right direction) or with a "why_not", so the replay can show both.
    """
    cfg = cfg or RiskConfig()
    level_prices = [lv["price"] for lv in levels]
    out = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        if None in (ema_l[i], basis[i], atr_v[i]):
            continue
        for lv in levels:
            r = evaluate_level(lv["price"], c, prev)
            if not r or r["outcome"] != "rejected" or r["strength"] != "pattern":
                continue
            side = "short" if r["bias"] == "bearish" else "long"
            stretch = (c["c"] - basis[i]) / atr_v[i]   # + above the averages, - below
            if side == "short":
                beyond = c["c"] > ema_l[i] and c["c"] > basis[i]
                qualified, why_not = beyond and stretch >= stretch_min, (
                    "price not above both lines" if not beyond else f"stretch {stretch:.1f} ATR < {stretch_min}")
            else:
                beyond = c["c"] < ema_l[i] and c["c"] < basis[i]
                qualified, why_not = beyond and -stretch >= stretch_min, (
                    "price not below both lines" if not beyond else f"stretch {-stretch:.1f} ATR < {stretch_min}")
            out.append({
                "setup": "stretched rejection",
                "i": i,
                "t": c["t"],
                "entry_time": _entry_time(c, minutes),
                "side": side,
                "level": lv,
                "rejection": r,
                "stretch_atr": round(stretch, 2),
                "qualified": qualified,
                "why_not": None if qualified else why_not,
                "detail": f"{', '.join(p for p in r['patterns'] if p != 'doji') or 'pattern'} at "
                          f"{lv['label'] or lv['price']}, {abs(stretch):.1f} ATR "
                          f"{'above' if stretch > 0 else 'below'} BB basis",
                "plan": plan_trade(lv["price"], r, c, level_prices, cfg),
            })
    return out
