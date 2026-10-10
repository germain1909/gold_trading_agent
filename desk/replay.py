"""
Replay a past session through the same level logic the live watcher uses,
to see which levels would have been rejected or accepted. Nothing is saved.

Run from the project root:
    python -m desk.replay NQ 2026-10-09 31170 31085.5 31001
    python -m desk.replay NQ 2026-10-09 31461.75:weekly-high 31252:VAH 31079.25:VAL
    python -m desk.replay NQ 2026-10-09          # uses your saved active levels

By default it replays the full 23-hour CME session (18:00 ET the day before
to 17:00 ET). Add --rth for just the NY cash session (9:30-16:00 ET).

After the candle log it prints a per-session scorecard (Asia / London / NY):
every rejection with how far price moved in its favor (MFE) and against it
(MAE) over the next --hold candles (default 4 = 2 hours on 30m), and a star
on the one trade the pick rule would take that session:
    --pick first      first rejection of the session (default)
    --pick pattern    first rejection graded [pattern]

Finally it simulates the two setups (desk/setups.py) through the day:
    1. trend cross          - close beyond both EMA line and BB basis; ride it
                              until a close back across the EMA line
    2. stretched rejection  - [pattern] rejection at a level while price is
                              stretched >= SETUP_STRETCH_ATR x ATR from the BB
                              basis; target the next level
Up to RISK_TRADES_PER_SESSION trades per session (default 2), one position at
a time, risk rules from desk/risk.py, with 1-minute bars deciding fills.
"""
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from topstep.client import TopstepClient
from . import store
from .patterns import evaluate_level, STRENGTHS
from .risk import RiskConfig, simulate, simulate_trail
from .indicators import ema_line, bb_basis, atr, EMA_LENGTH, EMA_SMOOTHING, BB_LENGTH, ATR_LENGTH
from .setups import trend_cross_signals, stretched_rejection_signals, STRETCH_MIN
from .sessions import session_of, SESSIONS
from .watcher import resolve_front_month, CANDLE_MINUTES, MIN_STRENGTH, px

ET = ZoneInfo("America/New_York")

GREEN = "\033[92m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def fetch_bars(client: TopstepClient, contract_id: str, start: datetime, end: datetime, minutes: int = CANDLE_MINUTES):
    client._ensure_token()
    resp = client._session.post(
        f"{client._base_url}/api/History/retrieveBars",
        headers={"Authorization": f"Bearer {client._token}"},
        json={
            "contractId": contract_id,
            "live": False,
            "startTime": start.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            "endTime": end.astimezone(ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
            "unit": 2,
            "unitNumber": minutes,
            "limit": 5000,
            "includePartialBar": False,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return sorted(resp.json().get("bars", []) or [], key=lambda b: b["t"])


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rth = "--rth" in sys.argv
    hold = int(next((a.split("=")[1] for a in sys.argv if a.startswith("--hold=")), 4))
    pick = next((a.split("=")[1] for a in sys.argv if a.startswith("--pick=")), "first")
    if pick not in ("first", "pattern"):
        raise SystemExit("--pick must be 'first' or 'pattern'")
    if len(args) < 2:
        raise SystemExit(__doc__)
    symbol, day = args[0].upper(), datetime.strptime(args[1], "%Y-%m-%d").date()

    if args[2:]:
        levels = []
        for a in args[2:]:
            price, _, label = a.partition(":")
            levels.append({"price": float(price), "label": label})
    else:
        levels = store.list_levels(symbol)
        if not levels:
            raise SystemExit(f"No saved {symbol} levels; pass prices on the command line.")

    if rth:
        start = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
        end = datetime.combine(day, datetime.min.time(), ET).replace(hour=16)
    else:
        start = datetime.combine(day - timedelta(days=1), datetime.min.time(), ET).replace(hour=18)
        end = datetime.combine(day, datetime.min.time(), ET).replace(hour=17)

    client = TopstepClient()
    contract = resolve_front_month(client, symbol)
    # Start a few days early so the EMAs are warmed up; end late so trades can be scored
    all_bars = fetch_bars(client, contract["id"], start - timedelta(days=5),
                          end + timedelta(minutes=CANDLE_MINUTES * hold) + timedelta(hours=1))
    bars = [b for b in all_bars if start <= datetime.fromisoformat(b["t"]) < end]
    if not bars:
        raise SystemExit(f"No bars for {symbol} on {day}.")

    print(f"\n{BOLD}{symbol} ({contract['name']}) {day} - {CANDLE_MINUTES}m candles, "
          f"{'NY cash session' if rth else 'full 23h session'}, min strength: {MIN_STRENGTH}{RESET}")
    print("Levels: " + ", ".join(f"{px(lv['price'])}{' ' + lv['label'] if lv['label'] else ''}" for lv in levels) + "\n")

    counts = {"rejected": 0, "accepted": 0, "ignored": 0}
    rejections = []
    prev = None
    for i, b in enumerate(bars):
        candle = {k: b[k] for k in "ohlc"}
        when = datetime.fromisoformat(b["t"]).astimezone(ET).strftime("%a %H:%M")
        events = []
        for lv in levels:
            r = evaluate_level(lv["price"], candle, prev)
            if r is None:
                continue
            name = f"{px(lv['price'])}{' (' + lv['label'] + ')' if lv['label'] else ''}"
            if r["outcome"] == "rejected" and STRENGTHS.index(r["strength"]) < STRENGTHS.index(MIN_STRENGTH):
                counts["ignored"] += 1
                events.append(f"{DIM}{name} closed back across, {r['strength']} wick - not a rejection{RESET}")
            elif r["outcome"] == "rejected":
                counts["rejected"] += 1
                rejections.append({"i": i, "bar": b, "level": name, "level_price": lv["price"], **r})
                color = GREEN if r["bias"] == "bullish" else RED
                events.append(f"{color}{BOLD}{name} REJECTED [{r['strength']}] as {r['role']} - {r['bias']}"
                              f"{', ' + ', '.join(r['patterns']) if r['patterns'] else ''}{RESET}")
            else:
                counts["accepted"] += 1
                events.append(f"{DIM}{name} accepted {'above' if r['bias'] == 'bullish' else 'below'} "
                              f"(recorded, no alert){RESET}")
        ohlc = f"O {px(b['o'])} H {px(b['h'])} L {px(b['l'])} C {px(b['c'])}"
        if not rth and when.endswith(("03:00", "09:30")):
            print(f"{BOLD}--- {'London' if when.endswith('03:00') else 'NY cash open'} ---{RESET}")
        print(f"{when}  {DIM}{ohlc}{RESET}")
        for e in events:
            print(f"        {e}")
        prev = candle

    print(f"\n{counts['rejected']} rejection alerts | {counts['accepted']} acceptances recorded without alert | "
          f"{counts['ignored']} weak close-backs ignored")

    print_scorecard(rejections, all_bars, hold, pick)

    minute_bars = fetch_bars(client, contract["id"], start, end, minutes=1)
    day_bars = [b for b in all_bars if datetime.fromisoformat(b["t"]) < end]   # warmup + the day
    simulate_setups(day_bars, levels, minute_bars, start)


def score(rej, all_bars, hold):
    """Points in favor (MFE) and against (MAE) over the next `hold` candles, entering at the close."""
    t0 = rej["bar"]["t"]
    after = [b for b in all_bars if b["t"] > t0][:hold]
    if not after:
        return None, None
    entry = rej["bar"]["c"]
    if rej["bias"] == "bullish":
        return max(b["h"] for b in after) - entry, entry - min(b["l"] for b in after)
    return entry - min(b["l"] for b in after), max(b["h"] for b in after) - entry


def print_scorecard(rejections, all_bars, hold, pick):
    print(f"\n{BOLD}Session scorecard - MFE/MAE over the next {hold} candles "
          f"({hold * CANDLE_MINUTES // 60}h{hold * CANDLE_MINUTES % 60 or ''}), pick rule: {pick}{RESET}")
    for session in SESSIONS:
        rows = [r for r in rejections if session_of(datetime.fromisoformat(r["bar"]["t"])) == session]
        print(f"\n{BOLD}{session}{RESET}")
        if not rows:
            print(f"  {DIM}no rejections - no trade{RESET}")
            continue
        candidates = rows if pick == "first" else [r for r in rows if r["strength"] == "pattern"]
        chosen = candidates[0] if candidates else None
        for r in rows:
            mfe, mae = score(r, all_bars, hold)
            when = datetime.fromisoformat(r["bar"]["t"]).astimezone(ET)
            closes = (when + timedelta(minutes=CANDLE_MINUTES)).strftime("%H:%M")
            star = f"{BOLD}★{RESET}" if r is chosen else " "
            result = f"+{px(mfe)} / -{px(mae)}" if mfe is not None else "n/a"
            pats = ", ".join(r["patterns"]) or "-"
            print(f"  {star} {closes}  {r['level']:<10} {r['bias']:<8} [{r['strength']}] {pats:<28} {result}")
        if chosen is None:
            print(f"  {DIM}no rejection matched the pick rule - no trade{RESET}")



def simulate_setups(candles, levels, minute_bars, start):
    cfg = RiskConfig()
    for b in candles:
        b["end"] = (datetime.fromisoformat(b["t"]) + timedelta(minutes=CANDLE_MINUTES)).isoformat()
    closes = [b["c"] for b in candles]
    ema_l, basis, atr_v = ema_line(closes), bb_basis(closes), atr(candles)
    exit_line = {b["t"]: e for b, e in zip(candles, ema_l)}
    level_prices = [lv["price"] for lv in levels]

    signals = trend_cross_signals(candles, ema_l, basis, CANDLE_MINUTES, levels, cfg, atr_v) + \
        stretched_rejection_signals(candles, ema_l, basis, atr_v, levels, CANDLE_MINUTES, cfg)
    # Only the replayed day, and never an entry at the 17:00 daily close
    signals = [s for s in signals
               if datetime.fromisoformat(s["t"]) >= start and session_of(s["entry_time"]) is not None]
    signals.sort(key=lambda s: (s["entry_time"], s["setup"]))

    print(f"\n{BOLD}Trades - 2 setups, max {cfg.trades_per_session} per session, one position at a time, "
          f"{'LOCK AND ROLL ON (buffer ' + format(cfg.lock_buffer_pts, 'g') + ' pts), ' if cfg.lock_roll else ''}"
          f"{cfg.min_contracts}-{cfg.max_contracts} {cfg.contract}, max risk ${cfg.max_risk_per_trade:,.0f}/trade{RESET}")
    print(f"{DIM}  trend cross: close beyond EMA {EMA_LENGTH} (SMA {EMA_SMOOTHING}) and BB basis (EMA {BB_LENGTH}), "
          f"stop beyond lines + {cfg.stop_buffer_pts:g}, target first level >= {cfg.min_rr}R, "
          f"else exit on close back across EMA line")
    print(f"  stretched rejection: [pattern] rejection >= {STRETCH_MIN} x ATR({ATR_LENGTH}) from BB basis, "
          f"stop beyond wick + {cfg.stop_buffer_pts:g}, target first level >= {cfg.min_rr}R{RESET}")

    day_pnl = 0.0
    flat_from = None
    for session in SESSIONS:
        rows = [s for s in signals if session_of(datetime.fromisoformat(s["t"])) == session]
        print(f"\n{BOLD}{session}{RESET}")
        taken = 0
        for sig in rows:
            when = sig["entry_time"].astimezone(ET).strftime("%H:%M")
            tag = f"{when} {sig['setup'].upper()} {sig['side']} ({sig['detail']})"
            plan = sig["plan"]
            if not sig["qualified"]:
                print(f"  {DIM}{tag} - not a setup: {sig['why_not']}{RESET}")
                continue
            if day_pnl <= -cfg.daily_loss_limit:
                print(f"  {DIM}{tag} - skipped: daily loss limit reached{RESET}")
                continue
            if taken >= cfg.trades_per_session:
                print(f"  {DIM}{tag} - skipped: session trade limit reached{RESET}")
                continue
            if flat_from and sig["entry_time"] < flat_from:
                print(f"  {DIM}{tag} - skipped: previous trade still open{RESET}")
                continue
            if not plan["ok"]:
                print(f"  {DIM}{tag} - skipped: {plan['reason']}{RESET}")
                continue

            entry_iso = sig["entry_time"].isoformat()
            if sig["setup"] == "trend cross":
                after = candles[sig["i"] + 1:]
                res = simulate_trail(plan, minute_bars, after, exit_line, cfg, level_prices)
            else:
                res = simulate(plan, [b for b in minute_bars if b["t"] >= entry_iso], cfg, level_prices)
            if res["exit"] is None:
                print(f"  {DIM}{tag} - skipped: no price data after entry{RESET}")
                continue
            taken += 1
            day_pnl += res["pnl_usd"]
            flat_from = datetime.fromisoformat(res["exit_time"]) + timedelta(minutes=1)

            target = (f"target {px(plan['target'])} ({px(plan['reward_pts'])} pts, {plan['rr']}R)"
                      if plan.get("target") else f"no level >= {cfg.min_rr}R away - ride until close back across EMA line")
            color = GREEN if res["pnl_usd"] > 0 else RED
            exit_at = datetime.fromisoformat(res["exit_time"]).astimezone(ET).strftime("%a %H:%M")
            print(f"  {BOLD}{when} {sig['setup'].upper()} - {plan['side'].upper()} {plan['contracts']} "
                  f"{plan['contract']}{RESET} ({sig['detail']})")
            print(f"        entry {px(plan['entry'])}  stop {px(plan['stop'])} ({px(plan['risk_pts'])} pts, "
                  f"-${plan['risk_usd']:,.0f})  {target}")
            for note in res.get("notes", []):
                print(f"        {DIM}{note}{RESET}")
            print(f"        {color}{BOLD}{res['how'].upper()} at {px(res['exit'])} ({exit_at}): "
                  f"{'+' if res['pnl_pts'] >= 0 else ''}{px(res['pnl_pts'])} pts = "
                  f"{'+' if res['pnl_usd'] >= 0 else '-'}${abs(res['pnl_usd']):,.0f}{RESET}")
        if taken == 0:
            print(f"  {DIM}no trade{RESET}")
    color = GREEN if day_pnl >= 0 else RED
    print(f"\n{BOLD}Day P&L: {color}{'+' if day_pnl >= 0 else '-'}${abs(day_pnl):,.0f}{RESET}")


if __name__ == "__main__":
    main()
