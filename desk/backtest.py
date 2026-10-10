"""
Backtest a range of days with levels computed from Topstep data - no manual levels.

For each trading day D:
  - prior day's profile (full CME session): pdVAH, pdPOC, pdVAL
  - older profiles (up to LOOKBACK trading days back): VAH/POC/VAL that sit
    within OLDER_RANGE_ATR x daily ATR of D's open, labelled with their date;
    skipped if within DEDUPE_PTS of a more recent level
  - ATR % levels from D's open, used beyond the value area (ATR_LEVELS=beyond
    unless overridden)

Each day then runs through desk.replay with the current strategy settings.

Run from the project root:
    python -m desk.backtest NQ 2026-10-05 2026-10-09
    python -m desk.backtest NQ 2026-10-05 2026-10-09 --show     # print each day's trades
"""
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, date

from topstep.client import TopstepClient
from .atr_levels import fetch_daily_bars, day_open_and_atr
from .profile import volume_profile, session_window, VALUE_AREA
from .replay import fetch_bars
from .watcher import resolve_front_month, px

LOOKBACK = int(os.getenv("BACKTEST_LOOKBACK_DAYS", 10))
OLDER_RANGE_ATR = float(os.getenv("BACKTEST_OLDER_RANGE_ATR", 1.0))
DEDUPE_PTS = float(os.getenv("BACKTEST_DEDUPE_PTS", 10))

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"


def build_levels(day: date, trading_days, profiles, day_open: float, atr_value: float):
    idx = trading_days.index(day)
    prior = trading_days[max(0, idx - 1 - LOOKBACK):idx][::-1]      # most recent first
    levels = []

    def add(price, label):
        if all(abs(price - lv[0]) > DEDUPE_PTS for lv in levels):
            levels.append((price, label))

    for k, d in enumerate(prior):
        p = profiles[d]
        if k == 0:
            add(p["vah"], "pdVAH")
            add(p["poc"], "pdPOC")
            add(p["val"], "pdVAL")
            continue
        tag = d.strftime("%m/%d")
        for key in ("vah", "poc", "val"):
            if abs(p[key] - day_open) <= OLDER_RANGE_ATR * atr_value:
                add(p[key], f"{key.upper()}-{tag}")
    return sorted(levels, reverse=True)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show = "--show" in sys.argv
    if len(args) < 3:
        raise SystemExit(__doc__)
    symbol = args[0].upper()
    first, last = (datetime.strptime(a, "%Y-%m-%d").date() for a in args[1:3])

    client = TopstepClient()
    contract = resolve_front_month(client, symbol)
    daily = fetch_daily_bars(client, contract["id"], last, lookback_days=(last - first).days + 200)
    trading_days = [datetime.fromisoformat(b["t"]).date() for b in daily]
    days = [d for d in trading_days if first <= d <= last]
    if not days:
        raise SystemExit("No trading days in that range.")

    need = set()
    for d in days:
        i = trading_days.index(d)
        need.update(trading_days[max(0, i - 1 - LOOKBACK):i])
    print(f"{DIM}Building volume profiles for {len(need)} days ({VALUE_AREA:.0%} value area, full session)...{RESET}")
    profiles = {d: volume_profile(fetch_bars(client, contract["id"], *session_window(d, "full"), minutes=1))
                for d in sorted(need)}

    env = {**os.environ, "ATR_LEVELS": os.getenv("ATR_LEVELS", "beyond")}
    total, rows, failed = 0.0, [], []
    for d in days:
        day_open, atr_value = day_open_and_atr(daily, d)
        levels = build_levels(d, trading_days, profiles, day_open, atr_value)
        level_args = [f"{px(p)}:{label}" for p, label in levels]
        proc = subprocess.run([sys.executable, "-m", "desk.replay", symbol, d.isoformat(), *level_args],
                              capture_output=True, text=True, env=env)
        clean = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)
        m = re.search(r"Day P&L: ([+-])\$([\d,]+)", clean)
        if not m:
            err = (proc.stderr.strip().splitlines() or ["no output"])[-1]
            failed.append(d)
            print(f"\n{BOLD}{d:%a %m/%d}{RESET}  {RED}FAILED - not counted: {err}{RESET}")
            continue
        pnl = (1 if m.group(1) == "+" else -1) * float(m.group(2).replace(",", ""))
        trades = [ln.strip() for ln in clean.splitlines() if re.search(r"(TARGET|STOP|TRAIL|TIME|HELD|ROLLED|LOCKED).* at .*pts = ", ln)]
        total += pnl
        rows.append((d, pnl, trades, levels))

        color = GREEN if pnl > 0 else RED if pnl < 0 else ""
        print(f"\n{BOLD}{d:%a %m/%d}{RESET}  {color}{'+' if pnl >= 0 else '-'}${abs(pnl):,.0f}{RESET}  "
              f"({len(trades)} trade{'s' if len(trades) != 1 else ''})")
        print(f"{DIM}  levels: " + ", ".join(f"{label} {px(p)}" for p, label in levels) + RESET)
        if show:
            block = clean[clean.find("Trades -"):] if "Trades -" in clean else ""
            for ln in block.splitlines()[3:]:
                if ln.strip() and "not a setup" not in ln:
                    print("  " + ln)
        else:
            for t in trades:
                print(f"  {t}")

    if not rows:
        raise SystemExit(f"{RED}Every day failed.{RESET}")
    wins = sum(1 for r in rows if r[1] > 0)
    losses = sum(1 for r in rows if r[1] < 0)
    n_trades = sum(len(r[2]) for r in rows)
    worst = min(r[1] for r in rows)
    color = GREEN if total >= 0 else RED
    print(f"\n{BOLD}Total {color}{'+' if total >= 0 else '-'}${abs(total):,.0f}{RESET}{BOLD} over {len(rows)} days, "
          f"{n_trades} trades | green days {wins}, red days {losses} | worst day {'+' if worst >= 0 else '-'}${abs(worst):,.0f}{RESET}")
    if failed:
        print(f"{RED}{BOLD}{len(failed)} day(s) FAILED and are not in the total: "
              f"{', '.join(f'{d:%m/%d}' for d in failed)} - re-run them{RESET}")


if __name__ == "__main__":
    main()
