"""
Trade journal tools: shadow outcomes, backfill and a text report.

Shadow outcomes: for every trigger in the journal - entered, skipped, refused
or failed - work out from 1-minute bars what the planned trade would have done
(stop or target first, best/worst excursion, minutes, P&L per 1 MNQ). Missed
trades then teach as much as taken ones.

Run from the project root:
    python -m desk.journal outcomes 2026-10-12            # compute shadow outcomes for a day
    python -m desk.journal show 2026-10-12                # print the day's journal
    python -m desk.journal backfill plans/2026-10-12.json 2026-10-12
                                                          # replay a day into the journal (source=replay), then outcomes
"""
import subprocess
import sys
from datetime import datetime, date
from typing import Dict, Any, List, Optional

from topstep.client import TopstepClient
from . import store
from .backtest import front_month_id
from .profile import session_window
from .replay import fetch_bars, ET

TRIGGER_EVENTS = ["entered", "skipped", "refused", "failed"]
MNQ_PER_POINT = 2.0


def shadow_outcome(plan: Dict[str, Any], bars: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Walk 1-minute bars (already starting at the entry time) for a planned trade.
    Stop wins if one bar touches both. Neither by the last bar -> "open" at its close.
    """
    if not bars or not all(k in plan for k in ("entry", "stop", "target", "side")):
        return None
    long = plan["side"] == "long"
    entry, stop, target = plan["entry"], plan["stop"], plan["target"]
    mfe = mae = 0.0
    start = datetime.fromisoformat(bars[0]["t"])
    for b in bars:
        mfe = max(mfe, (b["h"] - entry) if long else (entry - b["l"]))
        mae = max(mae, (entry - b["l"]) if long else (b["h"] - entry))
        hit_stop = b["l"] <= stop if long else b["h"] >= stop
        hit_target = b["h"] >= target if long else b["l"] <= target
        if hit_stop or hit_target:
            first, exit_price = ("stop", stop) if hit_stop else ("target", target)
            mins = int((datetime.fromisoformat(b["t"]) - start).total_seconds() // 60)
            break
    else:
        first, exit_price = "open", bars[-1]["c"]
        mins = int((datetime.fromisoformat(bars[-1]["t"]) - start).total_seconds() // 60)
    pnl = (exit_price - entry) if long else (entry - exit_price)
    return {"first_hit": first, "exit_price": exit_price, "minutes": mins, "mfe_pts": round(mfe, 2),
            "mae_pts": round(mae, 2), "pnl_pts": round(pnl, 2), "pnl_usd_1": round(pnl * MNQ_PER_POINT, 2)}


def compute_outcomes(day: str, symbol: str = "NQ", client: Optional[TopstepClient] = None) -> int:
    """Shadow outcomes for every trigger on `day`. Returns how many were computed."""
    triggers = [e for e in store.events(day, kinds=TRIGGER_EVENTS) if e.get("entry_time")]
    if not triggers:
        return 0
    d = date.fromisoformat(day)
    _, end = session_window(d, "full")
    client = client or TopstepClient()
    cid = front_month_id(symbol, d)
    first_entry = min(datetime.fromisoformat(e["entry_time"]) for e in triggers)
    bars = fetch_bars(client, cid, first_entry, min(end, datetime.now(ET)), minutes=1)
    done = 0
    for e in triggers:
        start = e["entry_time"]
        after = [b for b in bars if b["t"] >= start]
        out = shadow_outcome(e["plan"], after)
        if out:
            store.set_outcome(e["id"], out)
            done += 1
    return done


def show(day: str) -> None:
    evs = store.events(day)
    if not evs:
        print(f"No journal entries for {day}.")
        return
    print(f"Journal {day}: {len(evs)} event(s)")
    for e in evs:
        when = datetime.fromisoformat(e["entry_time"]).astimezone(ET).strftime("%H:%M") if e["entry_time"] else "     "
        p = e["plan"]
        plan = (f" {p.get('side', '').upper()} @ {p.get('entry')} stop {p.get('stop')} target {p.get('target')}"
                if p.get("entry") else "")
        print(f"  #{e['id']:<4} {when} [{e['source']}/{e['mode']}] {e['event'].upper():8} {e['scenario']}{plan}"
              f"{' - ' + e['reason'] if e['reason'] else ''}")
        o = store.outcome(e["id"])
        if o:
            print(f"        shadow: {o['first_hit']} after {o['minutes']} min, {o['pnl_pts']:+} pts "
                  f"(${o['pnl_usd_1']:+,.0f}/MNQ), best +{o['mfe_pts']} / worst -{o['mae_pts']}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        raise SystemExit(__doc__)
    cmd = args[0]
    if cmd == "outcomes" and len(args) == 2:
        print(f"Computed {compute_outcomes(args[1])} shadow outcome(s) for {args[1]}")
    elif cmd == "show" and len(args) == 2:
        show(args[1])
    elif cmd == "backfill" and len(args) == 3:
        plan, day = args[1], args[2]
        subprocess.run([sys.executable, "-m", "desk.plan_runner", plan, day], check=True)
        print(f"Computed {compute_outcomes(day)} shadow outcome(s) for {day}")
        show(day)
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
