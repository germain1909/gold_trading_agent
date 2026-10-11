# NQ plan - <DAY> <DATE>

Fill in the plain-English part, then write (or have an AI write) the JSON at
the bottom using the words and types in SCENARIOS.md. Save the JSON as
`plans/<YYYY-MM-DD>.json`. The desk reads every scenario back before arming it.

## Context
<!-- Where did the last session close relative to its value area? What kind of day was it? -->
- Prior day: closed at ____, (inside / above / below) value. Range ____ - ____.
- ATR (14-day): ____

## Levels
<!-- The prior-day levels are computed automatically; add anything else you trust. -->
- pdVAH ____ · pdPOC ____ · pdVAL ____
- Older levels worth watching: ____ (e.g. `POC 10/8`, `VAH 9/22`)
- Weekly high / low: ____ / ____
- ATR lines come from the session open (6 PM ET) automatically

## Bias
<!-- One or two lines: what do you expect, and what would change your mind? -->

## Scenarios
<!-- If-then statements in playbook language, e.g.:
  1. Asia reclaim: if price dips below pdVAL and reclaims it, go long to pdVAH.
  2. NY breakout: if it breaks above pdVAH in NY, go long to POC 10/8.
  3. Fade the top: if the day is up 50% and price rejects ATR+50% with a pattern, short to pdVAH.
  4. Trap: if it breaks below pdVAL and fails, long to pdPOC.
  5. Continuation: if trending down below pdVAL in London, short when it breaks the pause. -->
1.
2.
3.

## No-trade conditions
<!-- e.g. "chopping between pdVAL and pdVAH", "FOMC at 2 PM" -->

## Scenarios as JSON

```json
[
  {
    "name": "",
    "type": "fade | reclaim | lose | pullback | breakout | retest | trap | trend_turn | continuation_ema | continuation_pause",
    "side": "long | short",
    "level": "pdVAH | pdPOC | pdVAL | VAH 9/22 | ATR+50% | open | weekly high | <price>",
    "conditions": [],
    "session": "Asia | London | NY | any",
    "until": null,
    "target": "default",
    "stop": "default",
    "size": null,
    "rejection": "pattern",
    "once": true,
    "mode": "paper"
  }
]
```

Example: `plans/2026-10-12.json`.
