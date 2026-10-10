"""
Risk rules for rejection trades. Pure functions, no I/O, no LLM.

For a rejection candle:
  - entry:  the rejection candle's close
  - stop:   beyond the rejection wick (candle high for shorts, low for longs) + buffer
  - target: the first watched level in the trade's direction that is at least
            MIN_RR away (level to level; closer levels are passed over)
  - size:   the most MNQ contracts (between MIN and MAX, default 1-3) whose risk
            fits MAX_RISK; wide stops trade smaller instead of being skipped

A trade is skipped if the stop is too wide even at MIN contracts, there is no
level to target, or reward/risk is below MIN_RR.

All settings can be overridden with environment variables (see RiskConfig).
"""
import math
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, List, Optional


def _env(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass
class RiskConfig:
    contract: str = os.getenv("RISK_CONTRACT", "MNQ")
    point_value: float = _env("RISK_POINT_VALUE", 2.0)          # MNQ = $2/pt, NQ = $20/pt
    tick_size: float = _env("RISK_TICK_SIZE", 0.25)
    min_contracts: int = int(_env("RISK_MIN_CONTRACTS", 1))
    max_contracts: int = int(_env("RISK_MAX_CONTRACTS", 3))
    max_risk_per_trade: float = _env("RISK_MAX_PER_TRADE", 250)  # dollars
    stop_buffer_pts: float = _env("RISK_STOP_BUFFER", 2.0)       # beyond the wick
    min_rr: float = _env("RISK_MIN_RR", 1.5)
    min_target_pts: float = _env("RISK_MIN_TARGET_PTS", 10.0)    # ignore levels closer than this
    daily_loss_limit: float = _env("RISK_DAILY_LOSS_LIMIT", 1000)   # 50K combine
    trailing_drawdown: float = _env("RISK_TRAILING_DRAWDOWN", 2000)  # 50K combine
    trades_per_session: int = int(_env("RISK_TRADES_PER_SESSION", 2))
    # Lock and roll: at the target, don't exit - lock the stop just behind the
    # target; if the candle closes beyond it, ride to the next level.
    lock_roll: bool = os.getenv("RISK_LOCK_ROLL", "false").lower() == "true"
    lock_buffer_pts: float = _env("RISK_LOCK_BUFFER", 5.0)
    candle_minutes: int = int(_env("DESK_CANDLE_MINUTES", 30))


def round_to_tick(price: float, tick: float, up: bool) -> float:
    steps = price / tick
    return (math.ceil(steps) if up else math.floor(steps)) * tick


def choose_target(side: str, entry: float, stop: float, levels: List[float],
                  exclude: Optional[float] = None, cfg: Optional[RiskConfig] = None) -> Optional[float]:
    """First level in the trade's direction that gives at least min_rr (None if none does)."""
    cfg = cfg or RiskConfig()
    risk_pts = abs(entry - stop)
    if side == "short":
        candidates = sorted((p for p in levels if p < entry - cfg.min_target_pts and p != exclude), reverse=True)
    else:
        candidates = sorted(p for p in levels if p > entry + cfg.min_target_pts and p != exclude)
    for p in candidates:
        if risk_pts and abs(p - entry) / risk_pts >= cfg.min_rr:
            return p
    return None


def plan_trade(
    level_price: float,
    rejection: Dict[str, Any],
    candle: Dict[str, Any],
    levels: List[float],
    cfg: Optional[RiskConfig] = None,
) -> Dict[str, Any]:
    """
    Build a trade plan for a rejection.

    Args:
        level_price: the level that was rejected
        rejection: result of patterns.evaluate_level (needs "bias")
        candle: the rejection candle (o, h, l, c)
        levels: all watched level prices for the symbol
        cfg: risk settings

    Returns a dict with "ok" (bool). If ok: side, entry, stop, target, contracts,
    risk/reward in points and dollars, rr. If not ok: "reason".
    """
    cfg = cfg or RiskConfig()
    short = rejection["bias"] == "bearish"
    entry = candle["c"]

    if short:
        stop = round_to_tick(candle["h"] + cfg.stop_buffer_pts, cfg.tick_size, up=True)
    else:
        stop = round_to_tick(candle["l"] - cfg.stop_buffer_pts, cfg.tick_size, up=False)
    target = choose_target("short" if short else "long", entry, stop, levels, level_price, cfg)

    risk_pts = abs(entry - stop)
    plan: Dict[str, Any] = {
        "side": "short" if short else "long",
        "entry": entry,
        "stop": stop,
        "target": target,
        "risk_pts": risk_pts,
        "contract": cfg.contract,
    }

    if target is None:
        return {**plan, "ok": False, "reason": f"no level in the trade's direction at least {cfg.min_rr}R away"}

    reward_pts = abs(target - entry)
    rr = reward_pts / risk_pts if risk_pts else 0
    plan.update(reward_pts=reward_pts, rr=round(rr, 2))

    per_contract = risk_pts * cfg.point_value
    contracts = min(cfg.max_contracts, int(cfg.max_risk_per_trade // per_contract)) if per_contract else 0
    if contracts < cfg.min_contracts:
        return {**plan, "ok": False,
                "reason": f"stop too wide: {risk_pts:g} pts = ${per_contract * cfg.min_contracts:,.0f} at "
                          f"{cfg.min_contracts} {cfg.contract}, max ${cfg.max_risk_per_trade:,.0f}"}
    if rr < cfg.min_rr:
        return {**plan, "ok": False, "reason": f"reward/risk {rr:.2f} below minimum {cfg.min_rr}"}

    plan.update(
        ok=True,
        contracts=contracts,
        risk_usd=risk_pts * cfg.point_value * contracts,
        reward_usd=reward_pts * cfg.point_value * contracts,
    )
    return plan


def _bucket(t_iso: str, minutes: int) -> int:
    return int(datetime.fromisoformat(t_iso).timestamp() // (minutes * 60))


def _result(plan: Dict[str, Any], exit_price: float, how: str, when: str, cfg: RiskConfig,
            notes: Optional[List[str]] = None) -> Dict[str, Any]:
    short = plan["side"] == "short"
    pnl_pts = (plan["entry"] - exit_price) if short else (exit_price - plan["entry"])
    return {"exit": exit_price, "how": how, "exit_time": when, "pnl_pts": pnl_pts,
            "pnl_usd": pnl_pts * cfg.point_value * plan["contracts"], "notes": notes or []}


def lock_and_roll(plan: Dict[str, Any], bars: List[Dict[str, Any]], levels: List[float],
                  cfg: Optional[RiskConfig] = None) -> Dict[str, Any]:
    """
    Manage a trade from the 1-minute bar that first touched its target.

    At the touch the stop moves to just behind the target (lock_buffer_pts).
    When that 30m candle closes:
      - closed back inside the level -> exit at the close (the level held)
      - closed beyond it             -> stay in: stop just behind the nearest
                                        broken level, target the next level
    Repeats at each new target. With no next level, exits at the close.

    On the bar that touches a target, price came from the stop side, so that
    bar's extreme behind the target most likely happened before the touch.
    That bar is judged by its close: stopped only if it closed behind the lock.
    """
    cfg = cfg or RiskConfig()
    short = plan["side"] == "short"
    buf = cfg.lock_buffer_pts
    target = plan["target"]
    stop = target + buf if short else target - buf
    notes = []

    def t_hm(b):
        return datetime.fromisoformat(b["t"]).astimezone().strftime("%H:%M")

    def p(x):
        return f"{x:.2f}".rstrip("0").rstrip(".")

    i = 0
    notes.append(f"touched target {p(target)} at {t_hm(bars[0])}, stop locked at {p(stop)}")
    while True:
        # Finish the 30m candle in which the target was touched
        bucket = _bucket(bars[i]["t"], cfg.candle_minutes)
        j, last = i, None
        while j < len(bars) and _bucket(bars[j]["t"], cfg.candle_minutes) == bucket:
            b = bars[j]
            behind = (b["c"] >= stop if short else b["c"] <= stop) if j == i else \
                ((b["h"] >= stop) if short else (b["l"] <= stop))
            if behind:
                return _result(plan, stop, "locked stop", b["t"], cfg, notes)
            last = b
            j += 1
        if j >= len(bars):
            return _result(plan, last["c"], "time", last["t"], cfg, notes)
        close = last["c"]
        beyond = close < target if short else close > target
        if not beyond:
            notes.append(f"candle closed {p(close)}, back inside {p(target)} - level held")
            return _result(plan, close, "level held", last["t"], cfg, notes)

        # Level accepted: stop behind the nearest broken level, target the next one
        if short:
            broken = [p for p in levels if p > close and p <= target]
            ahead = sorted((p for p in levels if p < close - cfg.min_target_pts), reverse=True)
            stop = min(broken) + buf
        else:
            broken = [p for p in levels if p < close and p >= target]
            ahead = sorted(p for p in levels if p > close + cfg.min_target_pts)
            stop = max(broken) - buf
        if not ahead:
            notes.append(f"candle closed {p(close)} beyond {p(target)}, no next level - exit")
            return _result(plan, close, "rolled - no next level", last["t"], cfg, notes)
        target = ahead[0]
        notes.append(f"candle closed {p(close)} beyond the level - rolled: stop {p(stop)}, target {p(target)}")

        # Ride toward the next level
        while j < len(bars):
            b = bars[j]
            if (b["h"] >= stop) if short else (b["l"] <= stop):
                return _result(plan, stop, "rolled stop", b["t"], cfg, notes)
            if (b["l"] <= target) if short else (b["h"] >= target):
                stop = target + buf if short else target - buf
                notes.append(f"touched {p(target)} at {t_hm(b)}, stop locked at {p(stop)}")
                i = j
                break
            j += 1
        else:
            return _result(plan, bars[-1]["c"], "time", bars[-1]["t"], cfg, notes)


def simulate(plan: Dict[str, Any], bars_after: List[Dict[str, Any]], cfg: Optional[RiskConfig] = None,
             levels: Optional[List[float]] = None) -> Dict[str, Any]:
    """
    Walk 1-minute bars after entry to see whether stop or target was hit first.
    If a single bar touches both, assume the stop was hit (conservative).
    If neither is hit, exit at the last bar's close.
    With cfg.lock_roll and levels, a target touch hands over to lock_and_roll.
    """
    cfg = cfg or RiskConfig()
    short = plan["side"] == "short"
    for k, b in enumerate(bars_after):
        hit_stop = b["h"] >= plan["stop"] if short else b["l"] <= plan["stop"]
        hit_target = b["l"] <= plan["target"] if short else b["h"] >= plan["target"]
        if hit_stop:
            exit_price, how = plan["stop"], "stop"
            break
        if hit_target:
            if cfg.lock_roll and levels:
                return lock_and_roll(plan, bars_after[k:], levels, cfg)
            exit_price, how = plan["target"], "target"
            break
    else:
        if not bars_after:
            return {"exit": None, "how": "no data", "pnl_pts": 0.0, "pnl_usd": 0.0}
        b = bars_after[-1]
        exit_price, how = b["c"], "time"
    pnl_pts = (plan["entry"] - exit_price) if short else (exit_price - plan["entry"])
    return {
        "exit": exit_price,
        "how": how,
        "exit_time": b["t"],
        "pnl_pts": pnl_pts,
        "pnl_usd": pnl_pts * cfg.point_value * plan["contracts"],
    }


def size_position(side: str, entry: float, stop: float, levels: Optional[List[float]] = None,
                  cfg: Optional[RiskConfig] = None) -> Dict[str, Any]:
    """
    Size a trade whose stop isn't a candle wick (e.g. a trend cross). Target is
    the first level at least min_rr away; with no such level the trade has no
    target and rides until the trailing exit.
    """
    cfg = cfg or RiskConfig()
    risk_pts = abs(entry - stop)
    target = choose_target(side, entry, stop, levels or [], None, cfg)
    plan: Dict[str, Any] = {"side": side, "entry": entry, "stop": stop, "target": target,
                            "risk_pts": risk_pts, "contract": cfg.contract}
    if target is not None:
        plan.update(reward_pts=abs(target - entry), rr=round(abs(target - entry) / risk_pts, 2))
    per_contract = risk_pts * cfg.point_value
    contracts = min(cfg.max_contracts, int(cfg.max_risk_per_trade // per_contract)) if per_contract else 0
    if contracts < cfg.min_contracts:
        return {**plan, "ok": False,
                "reason": f"stop too wide: {risk_pts:g} pts = ${per_contract * cfg.min_contracts:,.0f} at "
                          f"{cfg.min_contracts} {cfg.contract}, max ${cfg.max_risk_per_trade:,.0f}"}
    plan.update(ok=True, contracts=contracts, risk_usd=per_contract * contracts)
    if target is not None:
        plan["reward_usd"] = plan["reward_pts"] * cfg.point_value * contracts
    return plan


def simulate_trail(
    plan: Dict[str, Any],
    minute_bars: List[Dict[str, Any]],
    candles: List[Dict[str, Any]],
    exit_line: Dict[str, Optional[float]],
    cfg: Optional[RiskConfig] = None,
    levels: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """
    Ride a trade until it hits its level target, its hard stop (both checked on
    1-minute bars; stop wins if one bar touches both), or a candle closes back
    across exit_line (keyed by candle "t").

    candles: the 30m candles after entry, each with "t" (start) and "end" (ISO) keys.
    """
    cfg = cfg or RiskConfig()
    short = plan["side"] == "short"
    exit_price = how = when = None
    for cd in candles:
        inside = [b for b in minute_bars if cd["t"] <= b["t"] < cd["end"]]
        target = plan.get("target")
        for b in inside:
            if (b["h"] >= plan["stop"]) if short else (b["l"] <= plan["stop"]):
                exit_price, how, when = plan["stop"], "stop", b["t"]
                break
            if target is not None and ((b["l"] <= target) if short else (b["h"] >= target)):
                if cfg.lock_roll and levels:
                    return lock_and_roll(plan, [m for m in minute_bars if m["t"] >= b["t"]], levels, cfg)
                exit_price, how, when = target, "target", b["t"]
                break
        if how:
            break
        line = exit_line.get(cd["t"])
        if line is not None and ((cd["c"] > line) if short else (cd["c"] < line)):
            exit_price, how, when = cd["c"], "trail", cd["end"]
            break
    else:
        if not candles:
            return {"exit": None, "how": "no data", "pnl_pts": 0.0, "pnl_usd": 0.0}
        exit_price, how, when = candles[-1]["c"], "time", candles[-1]["end"]
    pnl_pts = (plan["entry"] - exit_price) if short else (exit_price - plan["entry"])
    return {"exit": exit_price, "how": how, "exit_time": when, "pnl_pts": pnl_pts,
            "pnl_usd": pnl_pts * cfg.point_value * plan["contracts"]}
