"""
Trigger detector: decides when an armed scenario triggers, candle by candle.

One ScenarioWatch per armed scenario. Call on_candle() each time a 30m candle
closes; it updates what the scenario remembers (e.g. "price has traded below
VAL"), checks the session window, expiry and conditions, applies the trigger
rule for the scenario's type (SCENARIOS.md section 4), and returns a signal
with a trade plan when it fires.

The same code runs on past candles (desk.scenario_test) and live candles (the
watcher), so a scenario behaves the same in a backtest and in real time.
"""
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .indicators import crossed_for_entry
from .patterns import evaluate_level
from .risk import RiskConfig, round_to_tick, choose_target
from .scenarios import Scenario, resolve_level, SESSION_END
from .sessions import session_of
from .setups import STRETCH_MIN, CROSS_MIN_PRIOR, CROSS_BREAKOUT_BODY_ATR

ET = ZoneInfo("America/New_York")

BREAK_BODY_ATR = 0.5          # "breaks": body at least half the 30m ATR
RETEST_WINDOW = 6             # candles after a break in which a retest counts
TRAP_WINDOW = 2               # candles after a break in which a close back inside is a trap
TREND_MIN = 3                 # "trending": closes beyond both lines in a row
PAUSE_CANDLES = (2, 3, 4)     # "pauses": 2-4 candles...
PAUSE_MAX_ATR = 1.0           # ...whose combined range is at most one 30m ATR
FADE_DEFAULT_RANGE_PCT = 50   # fades need the day to have moved this far unless the scenario says otherwise


@dataclass
class Context:
    """Closed candles (oldest first, with indicator warmup) and today's levels."""
    candles: List[Dict[str, Any]]
    ema: List[Optional[float]]
    basis: List[Optional[float]]
    atr: List[Optional[float]]
    levels: Dict[str, float]
    day_open: Optional[float] = None
    day_atr: Optional[float] = None
    minutes: int = 30
    cfg: RiskConfig = field(default_factory=RiskConfig)


# ---------- helpers ----------

def _p(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


def candle_end(c: Dict[str, Any], minutes: int) -> datetime:
    return datetime.fromisoformat(c["t"]) + timedelta(minutes=minutes)


def minutes_into_day(dt: datetime) -> int:
    """Minutes since the 18:00 ET trading-day open (so Asia's midnight wrap sorts correctly)."""
    et = dt.astimezone(ET)
    return (et.hour * 60 + et.minute - 18 * 60) % 1440


def hm_into_day(hm: str) -> int:
    h, m = (int(x) for x in hm.split(":"))
    return (h * 60 + m - 18 * 60) % 1440


def trend_len(ctx: Context, i: int, side: str) -> int:
    """Closes in a row, ending at candle i, beyond both lines in the trade's direction."""
    n = 0
    for k in range(i, -1, -1):
        if not crossed_for_entry(side, ctx.candles[k]["c"], ctx.ema[k], ctx.basis[k]):
            break
        n += 1
    return n


def is_choppy(ctx: Context, i: int) -> bool:
    """Price crossed the EMA line 2+ times in the last 6 candles."""
    signs = [ctx.candles[k]["c"] > ctx.ema[k] for k in range(max(0, i - 5), i + 1) if ctx.ema[k] is not None]
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b) >= 2


def momentum(ctx: Context, i: int, side: str) -> bool:
    c, a = ctx.candles[i], ctx.atr[i]
    body = (c["c"] - c["o"]) if side == "long" else (c["o"] - c["c"])
    return a is not None and body >= BREAK_BODY_ATR * a


# ---------- trade plan ----------

def build_plan(sc: Scenario, side: str, entry: float, stop: float, ctx: Context,
               target_price: Optional[float], level_price: Optional[float],
               stop_beyond: Optional[float]) -> Dict[str, Any]:
    """Stop, target, size and R for a triggered scenario, using the global risk rules."""
    cfg = ctx.cfg
    long = side == "long"
    if stop_beyond is not None:
        stop = stop_beyond - cfg.stop_buffer_pts if long else stop_beyond + cfg.stop_buffer_pts
    stop = round_to_tick(stop, cfg.tick_size, up=not long)
    plan: Dict[str, Any] = {"side": side, "entry": entry, "stop": stop, "contract": cfg.contract}
    if (long and stop >= entry) or (not long and stop <= entry):
        return {**plan, "ok": False, "reason": f"stop {_p(stop)} is on the wrong side of entry {_p(entry)}"}
    risk_pts = abs(entry - stop)

    if target_price is None:
        target = choose_target(side, entry, stop, list(ctx.levels.values()), level_price, cfg)
        if target is None:
            return {**plan, "ok": False, "reason": f"no level at least {cfg.min_rr}R away"}
    else:
        target = target_price
        if (long and target <= entry) or (not long and target >= entry):
            return {**plan, "ok": False, "reason": f"target {_p(target)} is behind entry {_p(entry)}"}
    reward_pts = abs(target - entry)
    rr = reward_pts / risk_pts
    plan.update(target=target, risk_pts=risk_pts, reward_pts=reward_pts, rr=round(rr, 2))
    if rr < cfg.min_rr:
        return {**plan, "ok": False, "reason": f"reward/risk {rr:.2f} below {cfg.min_rr}"}

    per_contract = risk_pts * cfg.point_value
    if sc.size:
        contracts = sc.size
        if per_contract * contracts > cfg.max_risk_per_trade:
            return {**plan, "ok": False, "reason": f"{contracts} {cfg.contract} risks "
                                                   f"${per_contract * contracts:,.0f} > ${cfg.max_risk_per_trade:,.0f}"}
    else:
        contracts = min(cfg.max_contracts, math.floor(cfg.max_risk_per_trade / per_contract))
        if contracts < cfg.min_contracts:
            return {**plan, "ok": False, "reason": f"stop too wide: {_p(risk_pts)} pts = "
                                                   f"${per_contract * cfg.min_contracts:,.0f} at {cfg.min_contracts} "
                                                   f"{cfg.contract}"}
    plan.update(ok=True, contracts=contracts, risk_usd=per_contract * contracts,
                reward_usd=reward_pts * cfg.point_value * contracts)
    return plan


# ---------- the watch ----------

class ScenarioWatch:
    """Watches one armed scenario. Raises ScenarioError if a level isn't available."""

    def __init__(self, sc: Scenario, levels: Dict[str, float]):
        self.sc = sc
        self.side = sc.side
        self.long = sc.side == "long"
        self.level = resolve_level(sc.level, levels) if sc.level is not None else None
        self.target = None if sc.target == "default" else resolve_level(sc.target, levels)
        self.stop_beyond = None if sc.stop == "default" else resolve_level(sc.stop["beyond"], levels)
        self.conditions = [({**c, "level": resolve_level(c["level"], levels)} if "price" in c else c)
                           for c in sc.conditions]
        until = sc.until or SESSION_END.get(sc.session)
        self.until = hm_into_day(until) if until else None
        self.armed = True
        self.status = "armed"
        # memory for multi-candle triggers
        self.through = False        # reclaim/lose: price has traded through the level
        self.through_ext = None     # ...and the furthest it went
        self.break_i = None         # retest/trap: candle index of the break
        self.break_ext = None       # trap: furthest point of the failed break

    # -- window --

    def _in_window(self, c: Dict[str, Any], ctx: Context) -> bool:
        if self.sc.session != "any" and session_of(datetime.fromisoformat(c["t"])) != self.sc.session:
            return False
        return self.until is None or minutes_into_day(candle_end(c, ctx.minutes)) <= self.until

    def _expired(self, c: Dict[str, Any], ctx: Context) -> bool:
        """Past the expiry time (times count from the 18:00 open, so this works across midnight)."""
        return self.until is not None and minutes_into_day(candle_end(c, ctx.minutes)) > self.until

    # -- conditions --

    def _conditions_ok(self, ctx: Context, i: int) -> Tuple[bool, str]:
        c = ctx.candles[i]
        conds = list(self.conditions)
        if self.sc.type == "fade":
            if not any("stretched" in x for x in conds):
                conds.append({"stretched": True})
            if not any("day_range_pct" in x for x in conds):
                conds.append({"day_range_pct": FADE_DEFAULT_RANGE_PCT})
        for cond in conds:
            if "price" in cond:
                above = c["c"] > cond["level"]
                if above != (cond["price"] == "above"):
                    return False, f"close not {cond['price']} {_p(cond['level'])}"
            elif "trend" in cond:
                side = "long" if cond["trend"] == "up" else "short"
                if trend_len(ctx, i, side) < TREND_MIN:
                    return False, f"not trending {cond['trend']}"
            elif "stretched" in cond:
                if ctx.atr[i] is None or ctx.basis[i] is None:
                    return False, "indicators not ready"
                stretch = (c["c"] - ctx.basis[i]) / ctx.atr[i]          # + above the averages, - below
                # a fade is stretched AGAINST the trade (short a stretch up); anything else WITH it
                up = (not self.long) if self.sc.type == "fade" else self.long
                lines = (ctx.ema[i], ctx.basis[i])
                beyond = c["c"] > max(lines) if up else c["c"] < min(lines)
                if not beyond or (stretch if up else -stretch) < STRETCH_MIN:
                    return False, f"not stretched ({stretch:+.1f} ATR from basis)"
            elif "not_choppy" in cond:
                if is_choppy(ctx, i):
                    return False, "choppy"
            elif "day_range_pct" in cond:
                if ctx.day_open is None or not ctx.day_atr:
                    return False, "day open/ATR unknown"
                if self.sc.type == "fade":     # the day moved toward the level being faded
                    ext = c["h"] if not self.long else c["l"]
                    moved = (ext - ctx.day_open) * (1 if not self.long else -1) / ctx.day_atr * 100
                else:                          # the day moved in the trade's direction
                    moved = (c["c"] - ctx.day_open) * (1 if self.long else -1) / ctx.day_atr * 100
                if moved < cond["day_range_pct"]:
                    return False, f"day moved {moved:.0f}% of ATR (< {_p(cond['day_range_pct'])}%)"
        return True, ""

    # -- triggers: update memory, return (stop, detail) when the trigger rule fires --

    def _rejection(self, ctx, i, with_trend_check):
        c, prev = ctx.candles[i], ctx.candles[i - 1] if i else None
        r = evaluate_level(self.level, c, prev)
        if not r or r["outcome"] != "rejected" or r["bias"] != ("bullish" if self.long else "bearish"):
            return None
        if self.sc.rejection == "pattern" and r["strength"] != "pattern":
            return None
        if self.sc.rejection == "wick" and r["strength"] not in ("wick", "pattern"):
            return None
        if with_trend_check and trend_len(ctx, i, self.side) < TREND_MIN:
            return None
        stop = c["l"] - ctx.cfg.stop_buffer_pts if self.long else c["h"] + ctx.cfg.stop_buffer_pts
        pats = ", ".join(p for p in r["patterns"] if p != "doji") or r["strength"]
        return stop, f"{'held' if with_trend_check else 'rejected'} {_p(self.level)} ({pats})"

    def _trigger(self, ctx: Context, i: int) -> Optional[Tuple[float, str]]:
        t, L, c = self.sc.type, self.level, ctx.candles[i]
        buf = ctx.cfg.stop_buffer_pts
        prev = ctx.candles[i - 1] if i else None

        if t == "fade":
            return self._rejection(ctx, i, with_trend_check=False)

        if t == "pullback":
            return self._rejection(ctx, i, with_trend_check=True)

        if t in ("reclaim", "lose"):
            went_through = c["l"] < L if t == "reclaim" else c["h"] > L
            if went_through:
                ext = c["l"] if t == "reclaim" else c["h"]
                self.through_ext = ext if not self.through else (min(self.through_ext, ext) if t == "reclaim"
                                                                   else max(self.through_ext, ext))
                self.through = True
            closed_back = c["c"] > L if t == "reclaim" else c["c"] < L
            if self.through and closed_back:
                ext = self.through_ext
                self.through, self.through_ext = False, None
                stop = ext - buf if t == "reclaim" else ext + buf
                return stop, f"{'reclaimed' if t == 'reclaim' else 'lost'} {_p(L)} after trading to {_p(ext)}"
            return None

        if t == "breakout":
            if prev and (prev["c"] <= L < c["c"] if self.long else prev["c"] >= L > c["c"]) \
                    and momentum(ctx, i, self.side):
                mid = (c["h"] + c["l"]) / 2
                stop = min(L, mid) - buf if self.long else max(L, mid) + buf
                return stop, f"broke {'above' if self.long else 'below'} {_p(L)} with momentum"
            return None

        if t == "retest":
            if self.break_i is not None:
                age = i - self.break_i
                if age > RETEST_WINDOW or (c["c"] < L if self.long else c["c"] > L):
                    self.break_i = None          # retest window passed, or the break failed
                elif age >= 1 and (c["l"] <= L < c["c"] if self.long else c["h"] >= L > c["c"]):
                    self.break_i = None
                    stop = c["l"] - buf if self.long else c["h"] + buf
                    return stop, f"retested {_p(L)} {age} candle(s) after the break and held"
            if prev and (prev["c"] <= L < c["c"] if self.long else prev["c"] >= L > c["c"]) \
                    and momentum(ctx, i, self.side):
                self.break_i = i
            return None

        if t == "trap":
            # a long trap is a failed break BELOW the level (and vice versa)
            break_side = "short" if self.long else "long"
            if self.break_i is not None:
                self.break_ext = min(self.break_ext, c["l"]) if self.long else max(self.break_ext, c["h"])
                age = i - self.break_i
                if age > TRAP_WINDOW:
                    self.break_i = None
                elif age >= 1 and (c["c"] > L if self.long else c["c"] < L):
                    ext, self.break_i = self.break_ext, None
                    stop = ext - buf if self.long else ext + buf
                    return stop, f"failed break of {_p(L)} (went to {_p(ext)}), closed back inside"
            if prev and (prev["c"] >= L > c["c"] if self.long else prev["c"] <= L < c["c"]) \
                    and momentum(ctx, i, break_side):
                self.break_i = i
                self.break_ext = c["l"] if self.long else c["h"]
            return None

        if t == "trend_turn":
            now = crossed_for_entry(self.side, c["c"], ctx.ema[i], ctx.basis[i])
            before = prev is not None and crossed_for_entry(self.side, prev["c"], ctx.ema[i - 1], ctx.basis[i - 1])
            if not now or before:
                return None
            other = "short" if self.long else "long"
            prior = trend_len(ctx, i - 1, other) if i else 0
            body_atr = abs(c["c"] - c["o"]) / ctx.atr[i] if ctx.atr[i] else 0
            if prior < CROSS_MIN_PRIOR and body_atr < CROSS_BREAKOUT_BODY_ATR:
                return None
            lines = (ctx.ema[i], ctx.basis[i])
            stop = min(lines) - buf if self.long else max(lines) + buf
            return stop, f"trend turn ({prior} candles on the other side, body {body_atr:.2f} ATR)"

        if t == "continuation_ema":
            e = ctx.ema[i]
            if e is None or not i or trend_len(ctx, i - 1, self.side) < TREND_MIN:
                return None
            if (c["l"] <= e < c["c"]) if self.long else (c["h"] >= e > c["c"]):
                stop = c["l"] - buf if self.long else c["h"] + buf
                return stop, f"pulled back to the EMA ({e:.2f}) and resumed"
            return None

        if t == "continuation_pause":
            a = ctx.atr[i]
            if a is None:
                return None
            for k in PAUSE_CANDLES:
                start = i - k
                if start < 1:
                    continue
                pause = ctx.candles[start:i]
                hi, lo = max(p["h"] for p in pause), min(p["l"] for p in pause)
                if hi - lo > PAUSE_MAX_ATR * a or trend_len(ctx, start - 1, self.side) < TREND_MIN:
                    continue
                if (c["c"] > hi) if self.long else (c["c"] < lo):
                    stop = lo - buf if self.long else hi + buf
                    return stop, f"broke a {k}-candle pause ({_p(lo)}-{_p(hi)})"
            return None

        raise ValueError(f"no trigger rule for type {t}")

    # -- per candle --

    def on_candle(self, ctx: Context, i: int) -> Optional[Dict[str, Any]]:
        """Evaluate the candle that just closed. Returns a signal dict when the scenario triggers."""
        if not self.armed:
            return None
        c = ctx.candles[i]
        if self._expired(c, ctx):
            self.armed, self.status = False, "expired"
            return None
        if not self._in_window(c, ctx):
            return None
        fired = self._trigger(ctx, i)
        if fired is None:
            return None
        stop, detail = fired
        ok, why = self._conditions_ok(ctx, i)
        signal = {
            "scenario": self.sc.name,
            "type": self.sc.type,
            "side": self.side,
            "i": i,
            "t": c["t"],
            "entry_time": candle_end(c, ctx.minutes),
            "detail": detail,
        }
        if not ok:
            return {**signal, "plan": {"ok": False, "reason": f"condition not met: {why}"}}
        plan = build_plan(self.sc, self.side, c["c"], stop, ctx, self.target, self.level, self.stop_beyond)
        return {**signal, "plan": plan}

    def took_trade(self) -> None:
        if self.sc.once:
            self.armed, self.status = False, "triggered"
