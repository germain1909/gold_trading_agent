"""
Trading desk journal UI - a local web app over the trade journal.

    python -m desk.ui            # then open http://localhost:8050

Pages:
    /                 every trade on one page, grouped by day and labeled 10/12 #1, 10/12 #2, ...
    /10_12/1          short link: that day's first trade
    /days             days in the journal, with triggers and shadow P&L
    /day/<date>       every trigger that day: what the system did and the shadow outcome
    /trade/<id>       one trigger: chart, plan, context, outcome, AI reviews (local or Claude)
    /scorecard        shadow results by scenario type and session (taken or not)

Replays and live runs of the same trigger are shown once (live wins).
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta, date
from typing import Dict, Any, List

from flask import Flask, jsonify, render_template_string, request, abort

from . import store
from .scenarios import TYPES

app = Flask(__name__)
TRIGGERS = ["entered", "skipped", "refused", "failed"]

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} - Trading Desk</title>
<style>
:root { --bg:#0f1115; --panel:#171a21; --line:#272b35; --text:#e6e8ec; --dim:#8b92a1; --green:#22c55e;
        --red:#ef4444; --amber:#f59e0b; --blue:#60a5fa; }
@media (prefers-color-scheme: light) { :root:not([data-theme="dark"]) { --bg:#f6f7f9; --panel:#fff; --line:#e3e6eb;
        --text:#14171c; --dim:#5d6575; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif }
header { display:flex; gap:20px; align-items:center; padding:12px 20px; border-bottom:1px solid var(--line);
  background:var(--panel); position:sticky; top:0; z-index:5 }
header b { font-size:15px } header a { color:var(--dim); text-decoration:none } header a:hover { color:var(--text) }
main { max-width:1150px; margin:0 auto; padding:20px 16px 60px }
h1 { font-size:20px; margin:4px 0 16px } h2 { font-size:15px; margin:24px 0 10px; color:var(--dim);
  text-transform:uppercase; letter-spacing:.05em }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:14px }
table { width:100%; border-collapse:collapse } th, td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
  white-space:nowrap } th { color:var(--dim); font-weight:500; font-size:12px }
td.wrap { white-space:normal } tr:hover td { background:rgba(127,127,127,.06) } a { color:var(--blue) }
.tbl { overflow-x:auto } .num { text-align:right; font-variant-numeric:tabular-nums }
.pos { color:var(--green) } .neg { color:var(--red) } .dim { color:var(--dim) }
.tag { display:inline-block; padding:1px 8px; border-radius:99px; font-size:12px; border:1px solid var(--line) }
.tag.entered{border-color:var(--green);color:var(--green)} .tag.failed,.tag.refused{border-color:var(--red);color:var(--red)}
.tag.skipped{border-color:var(--amber);color:var(--amber)}
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:12px }
.kv div { display:flex; justify-content:space-between; gap:12px; padding:3px 0; border-bottom:1px dashed var(--line) }
.kv span:first-child { color:var(--dim) }
button { background:var(--panel); color:var(--text); border:1px solid var(--line); border-radius:8px; padding:8px 14px;
  cursor:pointer; font:inherit } button:hover { border-color:var(--blue) } button:disabled { opacity:.5; cursor:wait }
.review { white-space:pre-wrap; line-height:1.6 } #chart { height:420px }
</style></head><body>
<header><b>Trading Desk</b><a href="/">All trades</a><a href="/days">Days</a><a href="/scorecard">Scorecard</a></header>
<main>{{ body|safe }}</main></body></html>"""


def page(title: str, body: str, **ctx) -> str:
    return render_template_string(BASE, title=title, body=render_template_string(body, **ctx))


# ---------- data helpers ----------

def triggers(day: str = None) -> List[Dict[str, Any]]:
    """Trigger events, one per actual trigger (live row preferred over its replay copy)."""
    best: Dict[tuple, Dict[str, Any]] = {}
    for e in store.events(day, kinds=TRIGGERS):
        key = (e["day"], e["candle_t"], e["scenario"])
        if key not in best or (best[key]["source"] != "live" and e["source"] == "live"):
            best[key] = e
    out = sorted(best.values(), key=lambda e: (e["day"], e["entry_time"]))
    for e in out:
        e["outcome"] = store.outcome(e["id"])
        e["et"] = (datetime.fromisoformat(e["entry_time"]).astimezone(_et()).strftime("%H:%M")
                   if e["entry_time"] else "")
    return out


def _et():
    from .replay import ET
    return ET


def fmt_pts(x):
    return "" if x is None else f"{x:+.2f}".rstrip("0").rstrip(".")


app.jinja_env.filters["pts"] = fmt_pts


# ---------- pages ----------

def numbered(day: str) -> List[Dict[str, Any]]:
    """The day's triggers in time order, numbered 1, 2, 3... (the #n in 10/12 #1)."""
    ts = triggers(day)
    for n, t in enumerate(ts, 1):
        t["n"] = n
        t["label"] = f"{date.fromisoformat(day):%m/%d} #{n}"
        t["short_url"] = f"/{date.fromisoformat(day):%m_%d}/{n}"
    return ts


@app.route("/")
def all_trades():
    groups = [(d, numbered(d)) for d in store.journal_days()]
    groups = [(d, ts) for d, ts in groups if ts]
    return page("All trades", """
<h1>All trades</h1>
{% if not groups %}<div class="panel dim">No trades in the journal yet. The watcher writes them as scenarios
trigger; or backfill a day: <code>python -m desk.journal backfill plans/&lt;date&gt;.json &lt;date&gt;</code></div>{% endif %}
{% for d, ts in groups %}
<h2><a href="/day/{{ d }}">{{ d }}</a></h2>
<div class="panel tbl"><table>
{% for t in ts %}{% set o = t.outcome %}<tr>
<td><a href="{{ t.short_url }}"><b>{{ t.label }}</b></a></td>
<td>{{ t.et }}</td>
<td><a href="{{ t.short_url }}">{{ t.scenario }}</a></td>
<td>{{ t.side }}</td>
<td><span class="tag {{ t.event }}">{{ t.event }}</span></td>
<td>{{ o.first_hit if o else '' }}</td>
<td class="num {{ 'pos' if o and o.pnl_pts > 0 else 'neg' if o and o.pnl_pts < 0 else '' }}">{{ (o.pnl_pts|pts ~ ' pts') if o else '' }}</td>
<td class="num">{{ ('$' ~ '{:,.0f}'.format(o.pnl_usd_1)) if o else '' }}</td>
</tr>{% endfor %}
</table></div>
{% endfor %}
<p class="dim">Each link opens the trade: chart, plan, context, outcome and AI review. Short links work too,
e.g. <code>/10_12/1</code> for 10/12's first trade.</p>""", groups=groups)


@app.route("/<int:month>_<int:dom>/<int:n>")
def trade_by_label(month, dom, n):
    """Short links like /10_12/1: that day's nth trade (most recent year if several match)."""
    for d in store.journal_days():                       # newest first
        if date.fromisoformat(d).month == month and date.fromisoformat(d).day == dom:
            ts = numbered(d)
            if 1 <= n <= len(ts):
                return trade_view(ts[n - 1]["id"])
            abort(404, f"{d} has {len(ts)} trade(s)")
    abort(404, f"no journal day for {month}/{dom}")


@app.route("/days")
def days():
    rows = []
    for d in store.journal_days():
        ts = triggers(d)
        pnl = [t["outcome"]["pnl_usd_1"] for t in ts if t["outcome"]]
        rows.append({"day": d, "n": len(ts), "entered": sum(t["event"] == "entered" for t in ts),
                     "missed": sum(t["event"] in ("failed", "refused") for t in ts),
                     "skipped": sum(t["event"] == "skipped" for t in ts),
                     "shadow": sum(pnl) if pnl else None})
    return page("Days", """
<h1>Journal</h1>
{% if not rows %}<div class="panel dim">No journal entries yet. The watcher writes them as scenarios trigger;
or backfill a day: <code>python -m desk.journal backfill plans/&lt;date&gt;.json &lt;date&gt;</code></div>{% endif %}
<div class="panel tbl"><table>
<tr><th>Day</th><th class="num">Triggers</th><th class="num">Entered</th><th class="num">Missed (failed/refused)</th>
<th class="num">Skipped</th><th class="num">Shadow P&L / MNQ</th></tr>
{% for r in rows %}<tr><td><a href="/day/{{ r.day }}">{{ r.day }}</a></td><td class="num">{{ r.n }}</td>
<td class="num">{{ r.entered }}</td><td class="num">{{ r.missed }}</td><td class="num">{{ r.skipped }}</td>
<td class="num {{ 'pos' if (r.shadow or 0) > 0 else 'neg' if (r.shadow or 0) < 0 else '' }}">
{{ '' if r.shadow is none else ('+' if r.shadow >= 0 else '-') ~ '$' ~ '{:,.0f}'.format(r.shadow|abs) }}</td></tr>{% endfor %}
</table></div>
<p class="dim">Shadow P&L is what each planned trade did afterwards, taken or not, at 1 MNQ ($2/pt).</p>""", rows=rows)


@app.route("/day/<day>")
def day_view(day):
    ts = triggers(day)
    other = [e for e in store.events(day) if e["event"] in ("expired", "invalidated")]
    return page(day, """
<h1>{{ day }}</h1>
<h2>Triggers</h2>
<div class="panel tbl"><table>
<tr><th>Time ET</th><th>Scenario</th><th>Side</th><th>System</th><th>Entry</th><th>Stop</th><th>Target</th>
<th>Shadow</th><th class="num">Pts</th><th class="num">$ / MNQ</th></tr>
{% for t in ts %}{% set o = t.outcome %}<tr>
<td>{{ t.et }}</td><td><a href="/trade/{{ t.id }}">{{ t.scenario }}</a><br><span class="dim">{{ t.type }}</span></td>
<td>{{ t.side }}</td><td><span class="tag {{ t.event }}">{{ t.event }}</span> <span class="dim">{{ t.source }}/{{ t.mode }}</span></td>
<td>{{ t.plan.entry }}</td><td>{{ t.plan.stop }}</td><td>{{ t.plan.target }}</td>
<td>{{ o.first_hit if o else '-' }}</td>
<td class="num {{ 'pos' if o and o.pnl_pts > 0 else 'neg' if o and o.pnl_pts < 0 else '' }}">{{ (o.pnl_pts|pts) if o else '' }}</td>
<td class="num">{{ ('$' ~ '{:,.0f}'.format(o.pnl_usd_1)) if o else '' }}</td></tr>{% endfor %}
</table></div>
{% if other %}<h2>Expired / invalidated</h2><div class="panel tbl"><table>
{% for e in other %}<tr><td>{{ e.scenario }}</td><td><span class="tag">{{ e.event }}</span></td>
<td class="wrap dim">{{ e.reason }}</td></tr>{% endfor %}</table></div>{% endif %}""", day=day, ts=ts, other=other)


@app.route("/trade/<int:event_id>")
def trade_view(event_id):
    e = store.event(event_id)
    if not e:
        abort(404)
    o = store.outcome(event_id)
    reviews = store.analyses(event_id)
    ctx = e["context"]
    flat = {k: v for k, v in ctx.items() if not isinstance(v, dict)}
    nested = {k: v for k, v in ctx.items() if isinstance(v, dict)}
    return page(e["scenario"], """
<h1>{{ e.scenario }} <span class="tag {{ e.event }}">{{ e.event }}</span></h1>
<p class="dim">{{ e.day }} · {{ types.get(e.type, e.type) }} · {{ e.side }} · {{ e.source }}/{{ e.mode }}
{% if e.detail %} · {{ e.detail }}{% endif %}</p>
{% if e.reason %}<div class="panel {{ 'neg' if e.event in ('failed','refused') else '' }}">{{ e.reason }}</div>{% endif %}
<div class="panel"><div id="chart"></div><div class="dim" id="chart-note">Loading candles...</div></div>
<div class="grid">
 <div class="panel kv"><b>Plan</b>{% for k in ['side','entry','stop','target','rr','contracts','risk_usd'] %}
  {% if e.plan.get(k) is not none %}<div><span>{{ k }}</span><span>{{ e.plan[k] }}</span></div>{% endif %}{% endfor %}</div>
 <div class="panel kv"><b>Shadow outcome</b>{% if o %}
  <div><span>first hit</span><span>{{ o.first_hit }}</span></div><div><span>exit</span><span>{{ o.exit_price }}</span></div>
  <div><span>minutes</span><span>{{ o.minutes }}</span></div>
  <div><span>P&L</span><span class="{{ 'pos' if o.pnl_pts > 0 else 'neg' }}">{{ o.pnl_pts|pts }} pts · ${{ '{:,.0f}'.format(o.pnl_usd_1) }}/MNQ</span></div>
  <div><span>best / worst</span><span>+{{ o.mfe_pts }} / -{{ o.mae_pts }}</span></div>
  {% else %}<div class="dim">Not computed yet - <code>python -m desk.journal outcomes {{ e.day }}</code></div>{% endif %}</div>
 <div class="panel kv"><b>Context at trigger</b>{% for k, v in flat.items() %}<div><span>{{ k }}</span><span>{{ v }}</span></div>{% endfor %}
  {% for k, v in nested.items() if k != 'candle' %}<div><span>{{ k }}</span><span>{{ v }}</span></div>{% endfor %}</div>
</div>
<h2>AI review</h2>
<div class="panel"><button onclick="analyze('local')">Analyze (local LM Studio)</button>
<button onclick="analyze('claude')">Analyze (Claude)</button> <span class="dim" id="status"></span></div>
<div id="reviews">{% for r in reviews %}<div class="panel"><div class="dim">{{ r.model }} · {{ r.created_at }}</div>
<div class="review">{{ r.text }}</div></div>{% endfor %}</div>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<script>
const plan = {{ e.plan|tojson }}, levelPrice = {{ (e.context.get('level_price'))|tojson }};
const dark = !matchMedia('(prefers-color-scheme: light)').matches;
fetch('/api/trade/{{ e.id }}/candles').then(r => r.json()).then(d => {
  const el = document.getElementById('chart');
  if (!d.candles.length) { document.getElementById('chart-note').textContent = 'No candles available.'; return; }
  const chart = LightweightCharts.createChart(el, { height: 420, layout: { background: { color: 'transparent' },
    textColor: dark ? '#c9ced8' : '#333' }, grid: { vertLines: { color: dark ? '#22262f' : '#eee' },
    horzLines: { color: dark ? '#22262f' : '#eee' } }, timeScale: { timeVisible: true } });
  const s = chart.addCandlestickSeries();
  s.setData(d.candles);
  const line = (price, color, title) => price != null && s.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, title });
  line(plan.entry, '#60a5fa', 'entry'); line(plan.stop, '#ef4444', 'stop'); line(plan.target, '#22c55e', 'target');
  line(levelPrice, '#f59e0b', 'level');
  if (d.trigger) s.setMarkers([{ time: d.trigger, position: plan.side === 'long' ? 'belowBar' : 'aboveBar',
    color: '#f59e0b', shape: plan.side === 'long' ? 'arrowUp' : 'arrowDown', text: 'trigger' }]);
  chart.timeScale().fitContent();
  document.getElementById('chart-note').textContent = '30m candles (chart time is ET). Orange arrow = trigger candle.';
}).catch(() => document.getElementById('chart-note').textContent = 'Could not load candles.');
async function analyze(backend) {
  const st = document.getElementById('status');
  document.querySelectorAll('button').forEach(b => b.disabled = true);
  st.textContent = 'Analyzing with ' + backend + '...';
  try {
    const r = await fetch('/api/trade/{{ e.id }}/analyze?backend=' + backend, { method: 'POST' });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.statusText);
    const div = document.createElement('div'); div.className = 'panel';
    div.innerHTML = '<div class="dim"></div><div class="review"></div>';
    div.children[0].textContent = d.model + ' · just now'; div.children[1].textContent = d.text;
    document.getElementById('reviews').prepend(div); st.textContent = '';
  } catch (err) { st.textContent = 'Failed: ' + err.message; }
  document.querySelectorAll('button').forEach(b => b.disabled = false);
}
</script>""", e=e, o=o, reviews=reviews, flat=flat, nested=nested, types=TYPES)


@app.route("/scorecard")
def scorecard():
    ts = [t for t in triggers() if t["outcome"]]

    def group(key):
        g = defaultdict(list)
        for t in ts:
            g[key(t)].append(t["outcome"])
        rows = []
        for k, outs in sorted(g.items(), key=lambda kv: -len(kv[1])):
            pts = [o["pnl_pts"] for o in outs]
            rows.append({"key": k, "n": len(outs), "target": sum(o["first_hit"] == "target" for o in outs),
                         "stop": sum(o["first_hit"] == "stop" for o in outs),
                         "open": sum(o["first_hit"] == "open" for o in outs),
                         "win": sum(p > 0 for p in pts) / len(pts), "avg": sum(pts) / len(pts),
                         "usd": sum(o["pnl_usd_1"] for o in outs)})
        return rows

    sections = [("By scenario type", group(lambda t: TYPES.get(t["type"], t["type"]))),
                ("By session", group(lambda t: t["context"].get("session") or "?")),
                ("By type and session", group(lambda t: f"{t['type']} / {t['context'].get('session') or '?'}")),
                ("By what the system did", group(lambda t: t["event"]))]
    return page("Scorecard", """
<h1>Scorecard</h1>
<p class="dim">Shadow outcomes of every trigger, taken or not ({{ n }} so far). Small counts are anecdotes, not patterns.</p>
{% for title, rows in sections %}<h2>{{ title }}</h2><div class="panel tbl"><table>
<tr><th></th><th class="num">Triggers</th><th class="num">Target</th><th class="num">Stop</th><th class="num">Open</th>
<th class="num">Win %</th><th class="num">Avg pts</th><th class="num">$ / MNQ</th></tr>
{% for r in rows %}<tr><td>{{ r.key }}</td><td class="num">{{ r.n }}</td><td class="num">{{ r.target }}</td>
<td class="num">{{ r.stop }}</td><td class="num">{{ r.open }}</td><td class="num">{{ '{:.0%}'.format(r.win) }}</td>
<td class="num {{ 'pos' if r.avg > 0 else 'neg' }}">{{ r.avg|pts }}</td>
<td class="num">{{ ('+' if r.usd >= 0 else '-') ~ '$' ~ '{:,.0f}'.format(r.usd|abs) }}</td></tr>{% endfor %}
</table></div>{% endfor %}""", sections=sections, n=len(ts))


# ---------- api ----------

@app.route("/api/trade/<int:event_id>/candles")
def candles(event_id):
    e = store.event(event_id)
    if not e or not e["candle_t"]:
        return jsonify({"candles": [], "trigger": None})
    from topstep.client import TopstepClient
    from .backtest import front_month_id
    from .replay import fetch_bars
    t = datetime.fromisoformat(e["candle_t"])
    bars = fetch_bars(TopstepClient(), front_month_id(e["symbol"], date.fromisoformat(e["day"])),
                      t - timedelta(hours=8), t + timedelta(hours=8))

    def et_epoch(iso):  # lightweight-charts shows UTC; shift so the axis reads ET
        dt = datetime.fromisoformat(iso)
        return int(dt.timestamp() + dt.astimezone(_et()).utcoffset().total_seconds())

    return jsonify({"candles": [{"time": et_epoch(b["t"]), "open": b["o"], "high": b["h"], "low": b["l"],
                                 "close": b["c"]} for b in bars],
                    "trigger": et_epoch(e["candle_t"])})


@app.route("/api/trade/<int:event_id>/analyze", methods=["POST"])
def analyze_trade(event_id):
    from .analyst import analyze
    backend = request.args.get("backend", "local")
    try:
        return jsonify(analyze(event_id, backend))
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500


def main() -> None:
    port = int(os.getenv("DESK_UI_PORT", 8050))
    print(f"Trading desk UI: http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
