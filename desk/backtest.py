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

Each day uses the contract that was front month that day (quarterly NQ rolls
8 days before the third-Friday expiry), and its levels come from that contract.

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


MONTH_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}
ROOT_IDS = {"NQ": "CON.F.US.ENQ", "MNQ": "CON.F.US.MNQ", "ES": "CON.F.US.EP", "MES": "CON.F.US.MES"}


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def front_month_id(symbol: str, day: date) -> str:
    """Quarterly contract that was front month on `day` (rolls 8 days before expiry)."""
    root = ROOT_IDS.get(symbol)
    if not root:
        raise SystemExit(f"No quarterly contract mapping for {symbol}")
    for year in (day.year, day.year + 1):
        for month in (3, 6, 9, 12):
            if day < third_friday(year, month) - timedelta(days=8):
                return f"{root}.{MONTH_CODES[month]}{year % 100}"
    raise SystemExit(f"Could not find a contract for {day}")


def build_levels(day: date, trading_days, profiles, day_open: float, atr_value: float):
    idx = trading_days.index(day)
    prior = trading_days[max(0, idx - 1 - LOOKBACK):idx][::-1]      # most recent first
    levels = []

    def add(price, label):
        if all(abs(price - lv[0]) > DEDUPE_PTS for lv in levels):
            levels.append((price, label))

    prior = [d for d in prior if d in profiles]
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
    current = resolve_front_month(client, symbol)
    calendar = fetch_daily_bars(client, current["id"], last, lookback_days=(last - first).days + 200)
    trading_days = [datetime.fromisoformat(b["t"]).date() for b in calendar]
    days = [d for d in trading_days if first <= d <= last]
    if not days:
        raise SystemExit("No trading days in that range.")

    contract_of = {d: front_month_id(symbol, d) for d in days}
    dailies = {cid: fetch_daily_bars(client, cid, last, lookback_days=(last - first).days + 200)
               for cid in sorted(set(contract_of.values()))}
    need = set()
    for d in days:
        i = trading_days.index(d)
        need.update((contract_of[d], p) for p in trading_days[max(0, i - 1 - LOOKBACK):i])
    print(f"{DIM}Contracts: " + ", ".join(sorted(set(contract_of.values()))) +
          f" | building {len(need)} volume profiles ({VALUE_AREA:.0%} value area, full session)...{RESET}")
    profiles = {}
    for cid, p in sorted(need):
        bars = fetch_bars(client, cid, *session_window(p, "full"), minutes=1)
        if bars:
            profiles[(cid, p)] = volume_profile(bars)

    base_env = {**os.environ, "ATR_LEVELS": os.getenv("ATR_LEVELS", "beyond")}
    total, rows, failed = 0.0, [], []
    for d in days:
        cid = contract_of[d]
        try:
            day_open, atr_value = day_open_and_atr(dailies[cid], d)
        except ValueError as e:
            failed.append(d)
            print(f"\n{BOLD}{d:%a %m/%d}{RESET}  {RED}FAILED - not counted: {e}{RESET}")
            continue
        levels = build_levels(d, trading_days, {p: profiles[(c, p)] for (c, p) in profiles if c == cid},
                              day_open, atr_value)
        level_args = [f"{px(p)}:{label}" for p, label in levels]
        proc = subprocess.run([sys.executable, "-m", "desk.replay", symbol, d.isoformat(), *level_args],
                              capture_output=True, text=True, env={**base_env, "DESK_CONTRACT_ID": cid})
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
