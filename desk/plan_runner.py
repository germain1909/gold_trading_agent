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

Live trading: scenarios with "mode": "live" go through desk.broker when
LIVE_TRADING=true (real orders) or dryrun (print/Slack the order only).
Replays are always forced to dryrun - they never send real orders.
"""
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Dict, Any, List, Optional

from topstep.client import TopstepClient
from . import store
from .atr_levels import fetch_daily_bars, atr_levels
from .backtest import front_month_id, LOOKBACK
from .detector import Context, ScenarioWatch, trend_len
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
    """
    The CME trading day `now` belongs to. Sessions open at 18:00 ET the day before; the 17:00-18:00
    break counts as the next day, so a watcher started after the close arms the coming session.
    """
    et = now.astimezone(ET)
    d = et.date() + timedelta(days=1) if et.hour >= 17 else et.date()
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


# ---------- journal context ----------

CONFLUENCE_PTS = 25      # an ATR line this close to the scenario's level counts as confluence


def trade_context(ctx: Context, i: int, level_price: Optional[float], levels: Dict[str, float],
                  day_open: float, day_atr: float) -> Dict[str, Any]:
    """The clues at the trigger candle, recorded with every journal event."""
    c = ctx.candles[i]
    price = c["c"]
    t = datetime.fromisoformat(c["t"])
    atr30, basis, ema = ctx.atr[i], ctx.basis[i], ctx.ema[i]
    atr_lines = {k: v for k, v in levels.items() if k.startswith("ATR")}
    other = {k: v for k, v in levels.items() if not k.startswith("ATR")}
    near_atr = min(atr_lines.items(), key=lambda kv: abs(kv[1] - price)) if atr_lines else None
    near_lvl = min(other.items(), key=lambda kv: abs(kv[1] - price)) if other else None
    out = {
        "time_et": t.astimezone(ET).strftime("%H:%M"),
        "session": session_of(t),
        "close": price,
        "candle": {k: c[k] for k in ("o", "h", "l", "c")},
        "day_range_pct": round((price - day_open) / day_atr * 100) if day_atr else None,
        "stretch_atr": round((price - basis) / atr30, 2) if atr30 and basis is not None else None,
        "position_vs_lines": ("above both" if ema is not None and basis is not None and price > max(ema, basis)
                              else "below both" if ema is not None and basis is not None and price < min(ema, basis)
                              else "between"),
        "trend_up_candles": trend_len(ctx, i, "long"),
        "trend_down_candles": trend_len(ctx, i, "short"),
        "atr30": round(atr30, 1) if atr30 else None,
        "nearest_atr_line": {"label": near_atr[0], "dist": round(near_atr[1] - price, 2)} if near_atr else None,
        "nearest_level": {"label": near_lvl[0], "dist": round(near_lvl[1] - price, 2)} if near_lvl else None,
    }
    if level_price is not None and atr_lines:
        k, v = min(atr_lines.items(), key=lambda kv: abs(kv[1] - level_price))
        out["level_price"] = level_price
        out["confluence"] = {"atr_line": k, "dist": round(v - level_price, 2),
                             "yes": abs(v - level_price) <= CONFLUENCE_PTS}
    return out


# ---------- the runner ----------

class PlanRunner:
    def __init__(self, scenarios: List[Scenario], levels: Dict[str, float], day_open: float, day_atr: float,
                 symbol: str, contract: str, day: date, notifier: Optional[SlackNotifier] = None,
                 cfg: Optional[RiskConfig] = None, quiet: bool = False, broker=None,
                 source: str = "live", journal: bool = True):
        self.cfg = cfg or RiskConfig()
        self.notifier = notifier or SlackNotifier()
        self.symbol, self.contract, self.day = symbol, contract, day
        self.levels, self.day_open, self.day_atr = levels, day_open, day_atr
        self.quiet = quiet
        self.broker = broker
        self.source = source            # journal tag: live (watcher) or replay
        self.journal = journal
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
                self._log(sc, "invalidated", reason=f"not armed: {e}")
        key = ["pdVAH", "pdPOC", "pdVAL", "open", "weekly high", "weekly low"]
        level_txt = [f"{k} {_px(levels[k])}" for k in key if k in levels]
        self._say(f"{BOLD}Plan armed for {day} ({contract}): {len(self.watches)} scenario(s){RESET}")
        live = [w.sc.name for w in self.watches if w.sc.mode == "live"]
        if live:
            how = broker.describe() if broker else "no broker - these will paper trade"
            self._say(f"{RED}{BOLD}Live scenarios: {', '.join(live)} - {how}{RESET}")
            armed_lines.append(f"LIVE: {', '.join(live)} - {how}")
        for line in armed_lines:
            self._say(f"  {line}")
        self.notifier.send_session_started(symbol, contract, day.isoformat(), level_txt, armed_lines)

    def _say(self, text: str) -> None:
        if not self.quiet:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {text}")

    def _log(self, sc: Scenario, event: str, sig: Optional[Dict[str, Any]] = None, mode: str = "",
             reason: str = "", plan: Optional[Dict[str, Any]] = None, context: Optional[Dict[str, Any]] = None,
             result: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """Write one journal row. Never lets a journal problem affect trading."""
        if not self.journal:
            return None
        try:
            return store.add_event(
                self.day.isoformat(), self.symbol, sc.name, event,
                type=sc.type, side=sc.side, mode=mode or sc.mode, source=self.source,
                candle_t=sig["t"] if sig else "", entry_time=sig["entry_time"].isoformat() if sig else "",
                detail=sig["detail"] if sig else "", reason=reason, scenario_json=sc.to_dict(),
                plan=plan if plan is not None else (sig["plan"] if sig else {}),
                context=context or {}, result=result or {})
        except Exception as e:
            self._say(f"{RED}journal write failed: {e}{RESET}")
            return None

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
                self._log(w.sc, "expired", reason="expired without triggering")
            if not sig or warmup:
                continue
            context = trade_context(ctx, i, w.level, self.levels, self.day_open, self.day_atr)
            reason = self._blocked(sig)
            if reason:
                self._say(f"{YELLOW}{w.sc.name} triggered ({sig['detail']}) - no trade: {reason}{RESET}")
                self.notifier.send_scenario_skipped(self.symbol, w.sc.name, sig["detail"], reason)
                self._log(w.sc, "skipped", sig, reason=reason, context=context)
                continue
            self._enter(w, sig, context)

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

    def _enter(self, w: ScenarioWatch, sig: Dict[str, Any], context: Optional[Dict[str, Any]] = None) -> None:
        plan = sig["plan"]
        session = session_of(datetime.fromisoformat(sig["t"]))
        mode, order = "paper", None
        if w.sc.mode == "live" and self.broker and self.broker.active:
            size = self.broker.size
            per_point = self.broker.tick_value / self.broker.tick * size
            plan = {**plan, "contracts": size, "contract": self.broker.contract_name,
                    "risk_usd": plan["risk_pts"] * per_point, "reward_usd": plan["reward_pts"] * per_point}
            order = self.broker.enter(plan["side"], plan["entry"], plan["stop"], plan["target"], w.sc.name)
            if order is None:
                why = self.broker.last_error or "refused"
                self._say(f"{YELLOW}{w.sc.name} triggered ({sig['detail']}) - live entry {why}{RESET}")
                self._log(w.sc, "failed" if why.startswith("failed") else "refused", sig, mode="live",
                          reason=why, plan=plan, context=context)
                return
            mode = "dryrun" if order.get("dry") else "live"
        event_id = self._log(w.sc, "entered", sig, mode=mode, plan=plan, context=context,
                             result={"order": {k: v for k, v in (order or {}).items() if k != "payload"}})
        self.position = {"scenario": w.sc.name, "plan": plan, "mode": mode, "entry_time": sig["entry_time"],
                         "live": mode == "live", "order": order, "seen_open": False, "event_id": event_id,
                         "sc": w.sc, "sig": sig}
        self.taken[session] += 1
        w.took_trade()
        color = GREEN if plan["side"] == "long" else RED
        self._say(f"{color}{BOLD}ENTERED {plan['side'].upper()} {plan['contracts']} {plan['contract']} @ "
                  f"{_px(plan['entry'])} - {w.sc.name}{RESET}  stop {_px(plan['stop'])}  target "
                  f"{_px(plan['target'])} ({plan['rr']}R)  [{sig['detail']}]")
        self.notifier.send_trade_entered(self.symbol, w.sc.name, plan["side"], plan["contracts"], plan["contract"],
                                         plan["entry"], plan["stop"], plan["target"], plan["rr"],
                                         plan["risk_usd"], sig["detail"], mode)

    # -- prices --

    def on_price(self, price: float) -> None:
        """A live trade print. Manages the open paper position."""
        self.last_price = price
        if not self.position or self.position.get("live"):
            return          # live positions are closed by Topstep's bracket; see poll_live()
        p = self.position["plan"]
        long = p["side"] == "long"
        if (price <= p["stop"]) if long else (price >= p["stop"]):
            self._close(p["stop"], "stop")
        elif (price >= p["target"]) if long else (price <= p["target"]):
            self._close(p["target"], "target")

    def poll_live(self) -> None:
        """For a live position: has Topstep's bracket (or a flatten) closed it? Books the real fills."""
        pos = self.position
        if not pos or not pos.get("live") or not self.broker:
            return
        try:
            open_now = self.broker.open_position()
        except Exception as e:
            self._say(f"{RED}Could not check the live position: {e}{RESET}")
            return
        if open_now:
            pos["seen_open"] = True
            return
        placed = pos["order"]["placed_at"]
        if not pos["seen_open"] and (datetime.now(placed.tzinfo) - placed).total_seconds() < 20:
            return          # the market order may not have filled yet
        since = placed - timedelta(minutes=1)
        try:
            fill = self.broker.last_fill(since)
            pnl = self.broker.realized_since(since)
        except Exception as e:
            self._say(f"{RED}Live position is flat but fills couldn't be read: {e}{RESET}")
            fill, pnl = None, None
        exit_price = float(fill["price"]) if fill else (self.last_price or pos["plan"]["entry"])
        self._close(exit_price, "closed on Topstep", pnl_usd=pnl)

    def _close(self, exit_price: float, how: str, pnl_usd: Optional[float] = None) -> None:
        pos, p = self.position, self.position["plan"]
        pnl_pts = (exit_price - p["entry"]) if p["side"] == "long" else (p["entry"] - exit_price)
        if pnl_usd is None:
            pnl_usd = pnl_pts * self.cfg.point_value * p["contracts"]
        exit_info = {"exit": exit_price, "how": how, "pnl_pts": round(pnl_pts, 2), "pnl_usd": round(pnl_usd, 2),
                     "exit_time": datetime.now(ET).isoformat(timespec="seconds"), "entry_event": pos.get("event_id")}
        if pos.get("sc") is not None:
            self._log(pos["sc"], "closed", pos.get("sig"), mode=pos["mode"], plan=p, result=exit_info)
        if pos.get("event_id"):
            try:
                entry = store.event(pos["event_id"])
                store.update_event_result(pos["event_id"], {**(entry or {}).get("result", {}), "exit": exit_info})
            except Exception as e:
                self._say(f"{RED}journal update failed: {e}{RESET}")
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
        if self.position and self.position.get("live") and self.broker:
            self.broker.flatten("end of day")
            for _ in range(15):                     # give Topstep a moment to report the flat position
                self.poll_live()
                if not self.position:
                    break
                time.sleep(1)
        if self.position and self.last_price is not None and not self.position.get("live"):
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
    from .broker import LiveBroker
    # A replay never sends real orders: LIVE_TRADING=true or dryrun both become dryrun here.
    replay_mode = "dryrun" if os.getenv("LIVE_TRADING", "off").lower() in ("dryrun", "true") else "off"
    broker = LiveBroker(client, notifier, mode=replay_mode)
    cid = front_month_id(symbol, d)
    start, end = session_window(d, "full")
    candles = [b for b in fetch_bars(client, cid, start - timedelta(days=5), end) if datetime.fromisoformat(b["t"]) < end]
    first = next(k for k, b in enumerate(candles) if datetime.fromisoformat(b["t"]) >= start)
    levels, cid, day_atr = build_day_levels(client, symbol, d, candles[first]["o"])
    runner = PlanRunner(scenarios, levels, candles[first]["o"], day_atr, symbol, cid.split(".")[-1], d, notifier,
                        broker=broker, source="replay", journal="--no-journal" not in sys.argv)
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
