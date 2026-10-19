---
description: Build tomorrow's NQ trade plan - multi-timeframe brief, levels, scenarios, validated plan JSON
argument-hint: <YYYY-MM-DD> [your levels...] [notes]
---

Build the trade plan for the trading day and levels in: $ARGUMENTS
(first value = the day to plan for; any numbers = the trader's own levels; anything else = the trader's ideas or notes)

You are preparing a scenario plan for a discretionary NQ futures trader. The desk watches 30-minute candles and
acts only on scenarios written in the playbook's exact language. Your job is analysis and a draft plan;
the trader decides what goes live.

## 1. Gather the data
Run, from the project root:

    ~/venvs/py310/bin/python -m desk.market_brief <day> <the trader's levels>

It prints monthly / weekly / daily / 4H / 1H / 15m with RSI(14), MACD(12,26,9) and EMA20/50, the last five
days' VAH/POC/VAL, last week's high/low and composite profile, daily ATR, estimated ATR lines, and
confluences. If it says the prior session's levels are PROVISIONAL, say so in the plan and use
`pdVAH`/`pdPOC`/`pdVAL` names (the desk fills in final prices at arming time).

## 2. Read the rules and the evidence
- `SCENARIOS.md` - the vocabulary, the ten scenario types, the JSON format. Use only what it defines.
- `BRANCHES.md` - backtest evidence. Current read: on 30m, breakouts are the strongest generic
  scenario; traps and fades have done well in small samples; "lose pdVAH" has been weak - avoid it unless
  the trader asks for it.
- If the journal has data, check the scorecard numbers (`desk/ui.py` scorecard logic or
  `python -m desk.journal show <recent days>`) and mention anything relevant, with trade counts.

## 3. Write the analysis (top down, short)
- One line per timeframe: trend vs EMAs, RSI zone, MACD state and direction. Say plainly where the
  timeframes agree and where they conflict (e.g. "daily up but momentum fading; 4H down; 1H oversold").
- Key levels table, highest to lowest, marking confluences (two or more sources within ~25 pts) and the
  trader's own levels.
- Bias in one or two sentences, and what would change it.
- Calendar notes you are sure of (holidays, known scheduled events); otherwise say you didn't check.

## 4. Propose scenarios
- 4-7 scenarios, each one if-then sentence in playbook language, covering both directions where the
  levels support it. Prefer levels with confluence. Pair opposites at the same level when it makes sense
  (breakout vs trap).
- For each: the type, why it fits the multi-timeframe read, and the backtest evidence for that type.
- Flag any idea of the trader's that conflicts with the read - don't silently drop it.
- Add no-trade conditions (e.g. chop inside value).
- Sessions: Asia 18:00-03:00, London 03:00-09:30, NY 09:30-15:30 ET. Restrict a scenario to a session
  only for a reason.

## 5. Write and validate the plan file
- Save the scenarios to `plans/<day>.json` with every scenario `"mode": "live"` - the trader's
  default is live. Use `"paper"` only for a scenario the trader asked to keep on paper. If the file
  already exists, show the differences and ask before overwriting.
- Validate and read back with:

      ~/venvs/py310/bin/python -c "from desk.scenarios import load, describe; [print(describe(s)+'\n') for s in load(open('plans/<day>.json').read())]"

- Fix anything the loader rejects.
- Post the plan to Slack as a PDF (chart with the levels + every scenario read back):

      ~/venvs/py310/bin/python -m desk.plan_view <day> --slack

  If it reports a Slack problem, mention it in the report; the plan file is still good.

## 6. Report
Give the trader: the analysis, the levels table, the scenarios with reasoning, the read-back, and the
next steps - say which scenarios should be paper instead, then restart the watcher before 18:00 ET
(or let a running watcher pick up `plans/<day>.json` automatically when the session arms):

    pkill -f desk.watcher
    nohup caffeinate -ims python -u -m desk.watcher NQ --plan plans/<day>.json > watcher.log 2>&1 &

Be honest about uncertainty: these are scenarios to watch, not predictions, and most scenario types
still have little or no backtest evidence.
