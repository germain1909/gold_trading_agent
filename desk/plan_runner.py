"""
Runs a day's plan live: armed scenarios -> trigger detector -> global risk
rules -> paper position -> notifications (terminal + Slack).

The watcher feeds it closed 30m candles (on_candle) and live trade prices
(on_price). Events:
    plan armed            levels and scenarios for the day
    trade entered         scenario, entry, stop, target, size, R
    trade closed          target / stop / end of day, P&L
    triggered, no trade   the scenario fired but a rule blocked it
    invalidated           a level wasn't available, or the scenario expired
    daily limit           loss limit reached
    daily summary         at the end of the session

Replay a past day through exactly the same code (no SignalR needed):
    python -m desk.plan_runner plans/2026-10-12.json 2026-10-09
    python -m desk.plan_runner plans/2026-10-12.json 2026-10-09 --slack    # also post to Slack
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Dict, Any, List, Optional

from topstep.client import TopstepClient
from .atr_levels import fetch_daily_bars, atr_levels
from .backtest import front_month_id, LOOKBACK
from .detector import Context, ScenarioWatch
from .indicators import ema_line, bb_basis, atr
from .profile import volume_profile, session_window
from .replay import fetch_bars, ET
from .risk import RiskConfig
from .scenario_test import day_levels
from .scenarios import Scenario, ScenarioError, load, describe
from .sessions import session_of
from .slack_notifier import SlackNotifier

BOLD, DIM, GREEN, RED, YELLOW, RESET = "\033[1m", "\033[2m", "\033[92m", "\033[91m", "\033[93m", "\033[0m"


def _px(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".")


# ---------- levels for a trading day ----------

def trading_day_for(now: datetime) -> date:
    """The CME trading day `now` belongs to (sessions open at 18:00 ET the day before)."""
    et = now.astimezone(ET)
    d = et.date() + timedelta(days=1) if et.hour >= 18 else et.date()
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def build_day_levels(client: TopstepClient, symbol: str, d: date, day_open: float):
    """Named levels for trading day d, from data before it. Returns (levels, contract_id, daily ATR)."""
    cid = front_month_id(symbol, d)
    daily = [b for b in fetch_daily_bars(client, cid, d - timedelta(days=1), lookback_days=200)
             if datetime.fromisoformat(b["t"]).date() < d]
    day_atr = atr(daily, 14)[-1]
    trading_days = [datetime.fromisoformat(b["t"]).date() for b in daily] + [d]
    profiles = {}
    for p in trading_days[-1 - LOOKBACK:-1]:
        bars = fetch_bars(client, cid, *session_window(p, "full"), minutes=1)
        if bars:
            profiles[p] = volume_profile(bars)
    return day_levels(d, trading_days, profiles, daily, day_open, day_atr), cid, day_atr


# ---------- the runner ----------

class PlanRunner:
    def __init__(self, scenarios: List[Scenario], levels: Dict[str, float], day_open: float, day_atr: float,
                 symbol: str, contract: str, day: date, notifier: Optional[SlackNotifier] = None,
                 cfg: Optional[RiskConfig] = None, quiet: bool = False):
        self.cfg = cfg or RiskConfig()
        self.notifier = notifier or SlackNotifier()
        self.symbol, self.contract, self.day = symbol, contract, day
        self.levels, self.day_open, self.day_atr = levels, day_open, day_atr
        self.quiet = quiet
        self.watches: List[ScenarioWatch] = []
        self.position: Optional[Dict[str, Any]] = None
        self.taken: Dict[str, int] = defaultdict(int)
        self.day_pnl = 0.0
        self.closed: List[Dict[str, Any]] = []
        self.limit_hit = False
        self.ended = False
        self.last_price: Optional[float] = None

        armed_lines = []
        for sc in scenarios:
            try:
                self.watches.append(ScenarioWatch(sc, levels))
                armed_lines.append(describe(sc, levels).splitlines()[0].replace("Scenario: ", ""))
            except ScenarioError as e:
                self._say(f"{YELLOW}{sc.name}: not armed - {e}{RESET}")
                self.notifier.send_scenario_invalidated(symbol, sc.name, f"not armed: {e}")
        key = ["pdVAH", "pdPOC", "pdVAL", "open", "weekly high", "weekly low"]
        level_txt = [f"{k} {_px(levels[k])}" for k in key if k in levels]
        self._say(f"{BOLD}Plan armed for {day} ({contract}): {len(self.watches)} scenario(s){RESET}")
        for line in armed_lines:
            self._say(f"  {line}")
        self.notifier.send_session_started(symbol, contract, day.isoformat(), level_txt, armed_lines)

    def _say(self, text: str) -> None:
        if not self.quiet:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    # -- candles --

    def on_candle(self, ctx: Context, i: int, warmup: bool = False) -> None:
        """A 30m candle closed. With warmup=True only scenario memory is updated (no trades, no messages)."""
        if self.ended:
            return
        for w in self.watches:
            was_armed = w.armed
            sig = w.on_candle(ctx, i)
            if was_armed and not w.armed and w.status == "expired" and not warmup:
                self._say(f"{DIM}{w.sc.name}: expired without triggering{RESET}")
                self.notifier.send_scenario_invalidated(self.symbol, w.sc.name, "expired without triggering")
            if not sig or warmup:
                continue
            reason = self._blocked(sig)
            if reason:
                self._say(f"{YELLOW}{w.sc.name} triggered ({sig['detail']}) - no trade: {reason}{RESET}")
                self.notifier.send_scenario_skipped(self.symbol, w.sc.name, sig["detail"], reason)
                continue
            self._enter(w, sig)

    def _blocked(self, sig: Dict[str, Any]) -> Optional[str]:
        plan = sig["plan"]
        when = sig["entry_time"].astimezone(ET).strftime("%H:%M")
        if not plan["ok"]:
            return plan["reason"]
        if self.cfg.last_entry and self.cfg.last_entry <= when < "18:00":
            return f"no new entries after {self.cfg.last_entry}"
        if self.limit_hit:
            return "daily loss limit reached"
        session = session_of(datetime.fromisoformat(sig["t"]))
        if self.taken[session] >= self.cfg.trades_per_session:
            return f"{session} trade limit reached"
        if self.position:
            return f"already in a trade ({self.position['scenario']})"
        return None

    def _enter(self, w: ScenarioWatch, sig: Dict[str, Any]) -> None:
        plan = sig["plan"]
        session = session_of(datetime.fromisoformat(sig["t"]))
        self.position = {"scenario": w.sc.name, "plan": plan, "mode": w.sc.mode, "entry_time": sig["entry_time"]}
        self.taken[session] += 1
        w.took_trade()
        color = GREEN if plan["side"] == "long" else RED
        self._say(f"{color}{BOLD}ENTERED {plan['side'].upper()} {plan['contracts']} {plan['contract']} @ "
                  f"{_px(plan['entry'])} - {w.sc.name}{RESET}  stop {_px(plan['stop'])}  target "
                  f"{_px(plan['target'])} ({plan['rr']}R)  [{sig['detail']}]")
        self.notifier.send_trade_entered(self.symbol, w.sc.name, plan["side"], plan["contracts"], plan["contract"],
                                         plan["entry"], plan["stop"], plan["target"], plan["rr"],
                                         plan["risk_usd"], sig["detail"], w.sc.mode)

    # -- prices --

    def on_price(self, price: float) -> None:
        """A live trade print. Manages the open paper position."""
        self.last_price = price
        if not self.position:
            return
        p = self.position["plan"]
        long = p["side"] == "long"
        if (price <= p["stop"]) if long else (price >= p["stop"]):
            self._close(p["stop"], "stop")
        elif (price >= p["target"]) if long else (price <= p["target"]):
            self._close(p["target"], "target")

    def _close(self, exit_price: float, how: str) -> None:
        pos, p = self.position, self.position["plan"]
        pnl_pts = (exit_price - p["entry"]) if p["side"] == "long" else (p["entry"] - exit_price)
        pnl_usd = pnl_pts * self.cfg.point_value * p["contracts"]
        self.position = None
        self.day_pnl += pnl_usd
        self.closed.append({"scenario": pos["scenario"], "pnl": pnl_usd})
        color = GREEN if pnl_usd > 0 else RED
        self._say(f"{color}{BOLD}CLOSED {pos['scenario']}: {how} at {_px(exit_price)} = "
                  f"{'+' if pnl_usd >= 0 else '-'}${abs(pnl_usd):,.0f}{RESET}  (day {self.day_pnl:+,.0f})")
        self.notifier.send_trade_closed(self.symbol, pos["scenario"], how, exit_price, pnl_pts, pnl_usd)
        if self.day_pnl <= -self.cfg.daily_loss_limit and not self.limit_hit:
            self.limit_hit = True
            self._say(f"{RED}{BOLD}Daily loss limit reached - no more entries{RESET}")
            self.notifier.send_daily_limit_hit(f"day P&L at or below -${self.cfg.daily_loss_limit:,.0f}",
                                               self.day_pnl)

    # -- end of day --

    def end_of_day(self) -> None:
        if self.ended:
            return
        if self.position and self.last_price is not None:
            self._close(self.last_price, "end of day")
        self.ended = True
        unused = [w.sc.name for w in self.watches if w.armed]
        wins = sum(1 for t in self.closed if t["pnl"] > 0)
        losses = sum(1 for t in self.closed if t["pnl"] < 0)
        self._say(f"{BOLD}Day over: {self.day_pnl:+,.0f} from {len(self.closed)} trade(s); "
                  f"never triggered: {', '.join(unused) or 'none'}{RESET}")
        self.notifier.send_daily_summary(self.symbol, self.day.isoformat(), self.day_pnl, len(self.closed),
                                         wins, losses, unused)


# ---------- replay a past day through the live path ----------

def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 2:
        raise SystemExit(__doc__)
    plan_path, d, symbol = args[0], datetime.strptime(args[1], "%Y-%m-%d").date(), "NQ"
    scenarios = load(open(plan_path).read())
    notifier = SlackNotifier() if "--slack" in sys.argv else SlackNotifier(token="")

    client = TopstepClient()
    cid = front_month_id(symbol, d)
    start, end = session_window(d, "full")
    candles = [b for b in fetch_bars(client, cid, start - timedelta(days=5), end) if datetime.fromisoformat(b["t"]) < end]
    first = next(k for k, b in enumerate(candles) if datetime.fromisoformat(b["t"]) >= start)
    levels, cid, day_atr = build_day_levels(client, symbol, d, candles[first]["o"])
    runner = PlanRunner(scenarios, levels, candles[first]["o"], day_atr, symbol, cid.split(".")[-1], d, notifier)
    minute = fetch_bars(client, cid, start, end, minutes=1)

    closes = [b["c"] for b in candles]
    ctx = Context(candles, ema_line(closes), bb_basis(closes), atr(candles), levels, candles[first]["o"], day_atr)
    m = 0
    for i in range(first, len(candles)):
        candle_end = (datetime.fromisoformat(candles[i]["t"]) + timedelta(minutes=30)).isoformat()
        # prices that printed during this candle, in order (open, then high/low by bar color, then close)
        while m < len(minute) and minute[m]["t"] < candle_end:
            b = minute[m]
            path = (b["o"], b["l"], b["h"], b["c"]) if b["c"] >= b["o"] else (b["o"], b["h"], b["l"], b["c"])
            for px in path:
                runner.on_price(px)
            m += 1
        runner.on_candle(ctx, i)
    runner.end_of_day()


if __name__ == "__main__":
    main()
