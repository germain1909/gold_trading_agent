"""
A trade plan on the chart: recent 30m candles with every scenario's level drawn on them.
Used by the journal UI (/plan/<date>), the PDF, and Slack.

    python -m desk.plan_view 2026-10-19            # write plans/2026-10-19.pdf
    python -m desk.plan_view 2026-10-19 --slack    # ...and post it to the Slack channel

Before the session opens the ATR % lines are estimated from the last price (the desk uses the
real 18:00 open when it arms); the chart says so.
Slack upload needs the bot scope files:write (OAuth & Permissions, then reinstall the app).
"""
import os
import sys
from datetime import datetime, timedelta, timezone, date
from typing import Dict, Any, List, Optional

from .scenarios import load, describe, resolve_level, ScenarioError, TYPES
from .store import PROJECT_ROOT

PLANS_DIR = os.path.join(PROJECT_ROOT, "plans")
CONTEXT_LEVELS = ["pdVAH", "pdPOC", "pdVAL", "weekly high", "weekly low"]
HISTORY_DAYS = 4                         # calendar days of 30m candles before the session


def plan_path_for(day: date) -> str:
    return os.path.join(PLANS_DIR, f"{day.isoformat()}.json")


def _label(ref) -> str:
    return f"{ref:,.2f}".rstrip("0").rstrip(".") if isinstance(ref, float) else str(ref)


def plan_picture(day: date, path: Optional[str] = None, client=None) -> Dict[str, Any]:
    """Everything needed to draw the plan: candles, levels, and each scenario's level/target prices."""
    from topstep.client import TopstepClient
    from .plan_runner import build_day_levels
    from .profile import session_window
    from .replay import fetch_bars, ET
    from .backtest import front_month_id
    path = path or plan_path_for(day)
    with open(path) as f:
        scenarios = load(f.read())
    client = client or TopstepClient()
    cid = front_month_id("NQ", day)
    start, end = session_window(day, "full")
    now = datetime.now(timezone.utc)
    bars = fetch_bars(client, cid, start - timedelta(days=HISTORY_DAYS), min(now, end))
    if not bars:
        raise RuntimeError(f"no candles for {cid}")
    today = [b for b in bars if datetime.fromisoformat(b["t"]) >= start]
    estimated = not today
    day_open = today[0]["o"] if today else bars[-1]["c"]
    levels, cid, day_atr = build_day_levels(client, "NQ", day, day_open)

    items = []
    for sc in scenarios:
        it = {"name": sc.name, "type": sc.type, "type_name": TYPES.get(sc.type, sc.type), "side": sc.side,
              "mode": sc.mode, "session": sc.session, "level": None, "level_label": None, "target": None,
              "target_label": None, "error": None}
        try:
            it["text"] = describe(sc, levels)
        except ScenarioError as e:
            it["text"], it["error"] = describe(sc), str(e)
        if sc.level is not None:
            it["level_label"] = _label(sc.level)
            try:
                it["level"] = resolve_level(sc.level, levels)
            except ScenarioError as e:
                it["error"] = str(e)
        if sc.target not in (None, "default"):
            it["target_label"] = _label(sc.target)
            try:
                it["target"] = resolve_level(sc.target, levels)
            except ScenarioError:
                pass
        items.append(it)
    return {
        "day": day.isoformat(), "path": os.path.relpath(path, PROJECT_ROOT), "contract": cid,
        "session_start": start.astimezone(ET).isoformat(), "estimated": estimated, "day_open": day_open,
        "day_atr": day_atr, "last": bars[-1]["c"],
        "candles": [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars],
        "levels": {k: v for k, v in levels.items() if k in CONTEXT_LEVELS or k.startswith("ATR")},
        "scenarios": items,
    }


# ---------- PDF ----------

GREEN, RED, GRAY, AMBER = "#16a34a", "#dc2626", "#6b7280", "#d97706"


def render_pdf(pic: Dict[str, Any], out: str) -> str:
    """Page 1: candles with the plan's levels. Page 2: every scenario read back."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages
    from .replay import ET

    day = date.fromisoformat(pic["day"])
    candles = pic["candles"]
    times = [datetime.fromisoformat(c["t"]).astimezone(ET) for c in candles]
    n = len(candles)
    pad = 14                                                    # empty bars on the right for labels

    with PdfPages(out) as pdf:
        fig, ax = plt.subplots(figsize=(11, 8.5))
        fig.subplots_adjust(left=.04, right=.74, top=.88, bottom=.08)
        for i, c in enumerate(candles):
            up = c["c"] >= c["o"]
            col = "#26a69a" if up else "#ef5350"
            ax.vlines(i, c["l"], c["h"], color=col, linewidth=.7)
            ax.add_patch(plt.Rectangle((i - .35, min(c["o"], c["c"])), .7, max(abs(c["c"] - c["o"]), .25),
                                       color=col, linewidth=0))
        prices = [c["l"] for c in candles] + [c["h"] for c in candles]

        # context levels (dashed gray; levels within 10 pts share a label; far ones only in the note),
        # then each scenario level (solid, green long / red short)
        near = pic["day_atr"] * 1.2
        ctx = sorted((v, k + (" (est)" if k.startswith("ATR") and pic["estimated"] else ""))
                     for k, v in pic["levels"].items()
                     if (not k.startswith("ATR") or k in ("ATR+50%", "ATR-50%")) and abs(v - pic["last"]) <= near)
        far = [f"{k} {v:,.0f}" for k, v in pic["levels"].items()
               if not k.startswith("ATR") and abs(v - pic["last"]) > near]
        groups: List[List[tuple]] = []
        for v, k in ctx:
            if groups and v - groups[-1][-1][0] <= 10:
                groups[-1].append((v, k))
            else:
                groups.append([(v, k)])
        for g in groups:
            for v, k in g:
                ax.axhline(v, color=GRAY, linestyle=":" if k.startswith("ATR") else "--", linewidth=.8, alpha=.8)
                prices.append(v)
            ax.text(0, g[0][0], " / ".join(f"{k} {v:,.0f}" for v, k in g), ha="left", va="bottom", fontsize=6.5,
                    color=GRAY)
        by_level: Dict[float, List[Dict[str, Any]]] = {}
        for it in pic["scenarios"]:
            if it["level"] is not None:
                by_level.setdefault(round(it["level"], 2), []).append(it)
        for price, its in sorted(by_level.items()):
            col = GREEN if all(i["side"] == "long" for i in its) else RED if all(i["side"] == "short" for i in its) else AMBER
            ax.axhline(price, color=col, linewidth=1.6)
            label = "\n".join(f"{'▲' if i['side'] == 'long' else '▼'} {i['name']} ({i['type']}, "
                              f"{i['mode'].upper()})" for i in its)
            ax.text(n + pad - 1, price, f"{price:,.2f}\n{label}", ha="left", va="center", fontsize=7, color=col,
                    bbox=dict(facecolor="white", edgecolor=col, linewidth=.6, boxstyle="round,pad=.25"))
            prices.append(price)
        for it in pic["scenarios"]:
            if it["target"] is not None:
                ax.axhline(it["target"], color=GREEN if it["side"] == "long" else RED, linestyle=(0, (1, 3)),
                           linewidth=.8)
                prices.append(it["target"])

        sess = datetime.fromisoformat(pic["session_start"])
        x_open = next((i for i, t in enumerate(times) if t >= sess), n)
        ax.axvline(x_open - .5, color=AMBER, linewidth=.8)
        ax.text(x_open - .3, max(prices), " session open 18:00", fontsize=7, color=AMBER, va="top")
        ticks = [i for i, t in enumerate(times) if t.hour == 18 and t.minute == 0 or (t.hour == 9 and t.minute == 30)]
        ax.set_xticks(ticks)
        ax.set_xticklabels([times[i].strftime("%a %H:%M") for i in ticks], fontsize=7, rotation=0)
        ax.set_xlim(-1, n + pad)
        span = max(prices) - min(prices)
        ax.set_ylim(min(prices) - span * .03, max(prices) + span * .03)
        ax.yaxis.tick_right()
        ax.tick_params(axis="y", labelsize=7)
        ax.grid(alpha=.15)
        fig.suptitle(f"NQ plan - {day:%a %m/%d/%Y}  ({pic['contract']})", fontsize=14, x=.04, ha="left")
        note = (f"30m candles, ET. Last {pic['last']:,.2f}. Daily ATR {pic['day_atr']:,.0f}. "
                + ("ATR lines are estimated from the last price - the desk uses the real 18:00 open."
                   if pic["estimated"] else f"Day open {pic['day_open']:,.2f}.")
                + "\nSolid = scenario level (green long, red short); dotted = named targets; gray = context."
                + (f"  Off the chart: {', '.join(far)}." if far else ""))
        fig.text(.04, .9, note, fontsize=7.5, color=GRAY)
        pdf.savefig(fig)
        plt.close(fig)

        # page 2: the read-back, as the desk will run it
        fig = plt.figure(figsize=(8.5, 11))
        fig.text(.07, .96, f"NQ plan - {day:%a %m/%d/%Y}: scenarios", fontsize=13, weight="bold", va="top")
        fig.text(.07, .935, f"{pic['path']} · {len(pic['scenarios'])} scenario(s)", fontsize=8, color=GRAY, va="top")
        y = .905
        for it in pic["scenarios"]:
            col = GREEN if it["side"] == "long" else RED
            fig.text(.07, y, f"{'▲' if it['side'] == 'long' else '▼'} {it['name']}", fontsize=10, weight="bold",
                     color=col, va="top")
            fig.text(.93, y, it["mode"].upper(), fontsize=9, weight="bold", ha="right", va="top",
                     color=RED if it["mode"] == "live" else GRAY)
            y -= .022
            lines = [ln.replace("Size: 1-3 MNQ", "Size: 1 MNQ live (1-3 on paper)") if it["mode"] == "live" else ln
                     for ln in it["text"].splitlines()[1:]]
            if it["error"]:
                lines.append(f"NOT ARMED: {it['error']}")
            for line in lines:
                fig.text(.09, y, line.strip(), fontsize=7.5, family="monospace", va="top")
                y -= .0155
            y -= .012
            if y < .06:
                pdf.savefig(fig)
                plt.close(fig)
                fig, y = plt.figure(figsize=(8.5, 11)), .95
        fig.text(.07, .03, "Scenarios to watch, not predictions. Live trades: 1 MNQ, stop and target on Topstep.",
                 fontsize=7, color=GRAY)
        pdf.savefig(fig)
        plt.close(fig)
    return out


# ---------- Slack ----------

def post_pdf(pdf_path: str, day: date, notifier=None) -> str:
    """Upload the PDF to the Slack channel. Returns a short status message."""
    from .slack_notifier import SlackNotifier
    notifier = notifier or SlackNotifier()
    client = getattr(notifier, "_client", None)
    if client is None:
        return "Slack isn't configured (SLACK_BOT_TOKEN)."
    try:
        # files_upload_v2 needs a channel ID; posting the headline first gives us one
        head = client.chat_postMessage(channel=notifier._channel, text=f":bar_chart: *NQ plan for {day:%a %m/%d}*")
        client.files_upload_v2(channel=head["channel"], file=pdf_path, title=f"NQ plan {day.isoformat()}",
                               filename=os.path.basename(pdf_path), thread_ts=None)
        return f"Posted {os.path.basename(pdf_path)} to Slack."
    except Exception as e:
        msg = str(e)
        if "missing_scope" in msg:
            return ("Slack refused the upload: the bot needs the `files:write` scope. Add it under OAuth & "
                    "Permissions → Bot Token Scopes, then Reinstall to Workspace.")
        return f"Slack upload failed: {msg}"


def make_pdf(day: date, path: Optional[str] = None) -> str:
    out = os.path.join(PLANS_DIR, f"{day.isoformat()}.pdf")
    return render_pdf(plan_picture(day, path), out)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit(__doc__)
    day = date.fromisoformat(args[0])
    out = make_pdf(day, args[1] if len(args) > 1 else None)
    print(f"Wrote {out}")
    if "--slack" in sys.argv:
        print(post_pdf(out, day))


if __name__ == "__main__":
    main()
