"""
The two trade setups, as pure functions over closed 30m candles.

Setup 1 - TREND CROSS (ride it)
    A candle closes beyond BOTH the EMA line (EMA 15 smoothed SMA 5) and the
    BB basis (EMA 24) after the previous candle was not beyond both.
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


def _entry_time(candle: Dict[str, Any], minutes: int) -> datetime:
    return datetime.fromisoformat(candle["t"]) + timedelta(minutes=minutes)


def trend_cross_signals(
    candles: List[Dict[str, Any]],
    ema_l: List[Optional[float]],
    basis: List[Optional[float]],
    minutes: int,
    levels: Optional[List[Dict[str, Any]]] = None,
    cfg: Optional[RiskConfig] = None,
) -> List[Dict[str, Any]]:
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
                "detail": f"closed {'above' if side == 'long' else 'below'} EMA {ema_l[i]:.2f} / BB {basis[i]:.2f}",
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
