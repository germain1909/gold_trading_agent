"""
Test a plan (a scenarios JSON file) on past days, using the same trigger
detector the live watcher will use.

Each day the plan is armed fresh at the 18:00 ET open, with levels computed
from Topstep data:
    pdVAH / pdPOC / pdVAL       prior day's volume profile
    VAH-09/22, POC-10/01, ...   earlier days' profiles (last 10 trading days)
    ATR+25% ... ATR-100%        ATR % lines from the day's open
    open, weekly high/low

Global rules apply: max risk per trade, trades per session, one position at a
time, no entries after the cutoff, daily loss limit. Fills use 1-minute bars.

Run from the project root:
    python -m desk.scenario_test plans/2026-10-12.json 2026-09-01 2026-10-09
    python -m desk.scenario_test plans/2026-10-12.json 2026-09-01 2026-10-09 --show   # every signal
"""
import sys
from collections import defaultdict
from datetime import datetime, timedelta, date

from topstep.client import TopstepClient
from .atr_levels import fetch_daily_bars, day_open_and_atr, atr_levels
from .backtest import front_month_id, LOOKBACK
from .detector import Context, ScenarioWatch
from .indicators import ema_line, bb_basis, atr
from .profile import volume_profile, session_window
from .replay import fetch_bars, ET
from .risk import RiskConfig, simulate
from .scenarios import load, describe, ScenarioError
from .sessions import session_of
from .watcher import resolve_front_month, CANDLE_MINUTES, px

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[92m", "\033[91m", "\033[0m"


def day_levels(d: date, trading_days, profiles, daily, day_open: float, day_atr: float):
    """Every named level available on day d (label -> price)."""
    levels = {}
    i = trading_days.index(d)
    prior = [p for p in trading_days[max(0, i - 1 - LOOKBACK):i][::-1] if p in profiles]
    for k, p in enumerate(prior):
        pr = profiles[p]
        if k == 0:
            levels.update(pdVAH=pr["vah"], pdPOC=pr["poc"], pdVAL=pr["val"])
        tag = p.strftime("%m/%d")
        levels.update({f"VAH-{tag}": pr["vah"], f"POC-{tag}": pr["poc"], f"VAL-{tag}": pr["val"]})
    for lv in atr_levels(day_open, day_atr):
        levels[lv["label"]] = lv["price"]
    levels["open"] = day_open
    prev_week = (d - timedelta(days=7)).isocalendar()[:2]
    week = [b for b in daily if datetime.fromisoformat(b["t"]).date().isocalendar()[:2] == prev_week]
    if week:
        levels["weekly high"] = max(b["h"] for b in week)
        levels["weekly low"] = min(b["l"] for b in week)
    return levels


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show = "--show" in sys.argv
    if len(args) < 3:
        raise SystemExit(__doc__)
    plan_path, symbol = args[0], "NQ"
    first, last = (datetime.strptime(a, "%Y-%m-%d").date() for a in args[1:3])
    scenarios = load(open(plan_path).read())
    cfg = RiskConfig()

    print(f"{BOLD}Plan: {plan_path} - {len(scenarios)} scenario(s){RESET}")
    for sc in scenarios:
        print(DIM + describe(sc) + RESET)

    client = TopstepClient()
    current = resolve_front_month(client, symbol)
    calendar = fetch_daily_bars(client, current["id"], last, lookback_days=(last - first).days + 200)
    trading_days = [datetime.fromisoformat(b["t"]).date() for b in calendar]
    days = [d for d in trading_days if first <= d <= last]
    if not days:
        raise SystemExit("No trading days in that range.")
    contract_of = {d: front_month_id(symbol, d) for d in days}
    dailies = {cid: fetch_daily_bars(client, cid, last, lookback_days=(last - first).days + 200)
               for cid in set(contract_of.values())}
    need = set()
    for d in days:
        i = trading_days.index(d)
        need.update((contract_of[d], p) for p in trading_days[max(0, i - 1 - LOOKBACK):i])
    print(f"{DIM}Building {len(need)} volume profiles...{RESET}")
    profiles = {}
    for cid, p in sorted(need):
        bars = fetch_bars(client, cid, *session_window(p, "full"), minutes=1)
        if bars:
            profiles[(cid, p)] = volume_profile(bars)

    day_results, trades = [], []
    for d in days:
        cid = contract_of[d]
        try:
            day_open, day_atr = day_open_and_atr(dailies[cid], d)
        except ValueError as e:
            print(f"\n{BOLD}{d:%a %m/%d}{RESET} {RED}skipped: {e}{RESET}")
            continue
        levels = day_levels(d, trading_days, {p: v for (c, p), v in profiles.items() if c == cid},
                            dailies[cid], day_open, day_atr)
        start, end = session_window(d, "full")
        candles = [b for b in fetch_bars(client, cid, start - timedelta(days=5), end)
                   if datetime.fromisoformat(b["t"]) < end]
        closes = [b["c"] for b in candles]
        ctx = Context(candles, ema_line(closes), bb_basis(closes), atr(candles), levels,
                      day_open, day_atr, CANDLE_MINUTES, cfg)
        minute = fetch_bars(client, cid, start, end, minutes=1)

        log, watches = [], []
        for sc in scenarios:
            try:
                watches.append(ScenarioWatch(sc, levels))
            except ScenarioError as e:
                log.append(f"  {DIM}{sc.name}: not armed - {e}{RESET}")

        taken, day_pnl, flat_from = defaultdict(int), 0.0, None
        for i, c in enumerate(candles):
            if datetime.fromisoformat(c["t"]) < start:
                continue
            for w in watches:
                sig = w.on_candle(ctx, i)
                if not sig:
                    continue
                when = sig["entry_time"].astimezone(ET).strftime("%H:%M")
                tag = f"{when} {sig['scenario']} ({sig['type']} {sig['side']}): {sig['detail']}"
                plan = sig["plan"]
                session = session_of(datetime.fromisoformat(sig["t"]))
                skip = None
                if not plan["ok"]:
                    skip = plan["reason"]
                elif cfg.last_entry and cfg.last_entry <= when < "18:00":
                    skip = f"no new entries after {cfg.last_entry}"
                elif day_pnl <= -cfg.daily_loss_limit:
                    skip = "daily loss limit reached"
                elif taken[session] >= cfg.trades_per_session:
                    skip = f"{session} trade limit reached"
                elif flat_from and sig["entry_time"] < flat_from:
                    skip = "previous trade still open"
                if skip:
                    if show:
                        log.append(f"  {DIM}{tag} - skipped: {skip}{RESET}")
                    continue
                res = simulate(plan, [b for b in minute if b["t"] >= sig["entry_time"].isoformat()], cfg,
                               list(levels.values()) if cfg.lock_roll else None)
                if res["exit"] is None:
                    continue
                w.took_trade()
                taken[session] += 1
                day_pnl += res["pnl_usd"]
                flat_from = datetime.fromisoformat(res["exit_time"]) + timedelta(minutes=1)
                trades.append({"day": d, "scenario": sig["scenario"], "pnl": res["pnl_usd"]})
                color = GREEN if res["pnl_usd"] > 0 else RED
                exit_at = datetime.fromisoformat(res["exit_time"]).astimezone(ET).strftime("%H:%M")
                log.append(f"  {BOLD}{tag}{RESET}")
                log.append(f"        {plan['side'].upper()} {plan['contracts']} {plan['contract']} @ {px(plan['entry'])}"
                           f"  stop {px(plan['stop'])}  target {px(plan['target'])} ({plan['rr']}R)  ->  "
                           f"{color}{res['how']} {px(res['exit'])} at {exit_at}: "
                           f"{'+' if res['pnl_usd'] >= 0 else '-'}${abs(res['pnl_usd']):,.0f}{RESET}")
        day_results.append((d, day_pnl))
        if log or show:
            color = GREEN if day_pnl > 0 else RED if day_pnl < 0 else ""
            print(f"\n{BOLD}{d:%a %m/%d}{RESET}  {color}{'+' if day_pnl >= 0 else '-'}${abs(day_pnl):,.0f}{RESET}")
            print("\n".join(log) if log else f"  {DIM}no signals{RESET}")

    total = sum(p for _, p in day_results)
    eq = peak = dd = 0.0
    for _, p in day_results:
        eq += p
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    color = GREEN if total >= 0 else RED
    print(f"\n{BOLD}Total {color}{'+' if total >= 0 else '-'}${abs(total):,.0f}{RESET}{BOLD} over {len(day_results)} days, "
          f"{len(trades)} trades, win rate {len(wins) / len(trades):.0%}, max drawdown ${dd:,.0f}{RESET}"
          if trades else f"\n{BOLD}No trades over {len(day_results)} days.{RESET}")
    by = defaultdict(list)
    for t in trades:
        by[t["scenario"]].append(t["pnl"])
    for name, pnls in sorted(by.items(), key=lambda kv: sum(kv[1])):
        w = sum(1 for p in pnls if p > 0)
        print(f"  {name:28} {len(pnls):3} trades  {sum(pnls):+9,.0f}  win {w / len(pnls):.0%}")


if __name__ == "__main__":
    main()
