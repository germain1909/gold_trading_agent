# Scenario Playbook (draft)

The exact language for giving the desk a scenario. Each phrase has one meaning,
so the AI turns it into the same rules every time. The AI always reads the
scenario back as rules and waits for **"arm it"** before watching for it.

All candles are **30-minute closes** unless a scenario says otherwise.
Times are US Eastern.

---

## 1. Words for levels

| Say | Means |
|---|---|
| `VAH`, `POC`, `VAL` | Prior day's value area high / point of control / value area low (full CME session) |
| `pdVAH`, `pdPOC`, `pdVAL` | Same as above, written explicitly |
| `VAH 9/22`, `POC 10/1` | That day's level |
| `weekly high`, `weekly low` | Last week's high / low |
| `ATR+25%`, `ATR-50%`, ... | Your ATR % lines from today's open (±25 / 50 / 75 / 100%) |
| `the open` | Today's trading-day open (18:00 ET) |
| `31018` | Any price you name |

## 2. Words for what price does at a level

| Say | Exact meaning |
|---|---|
| **touches** X | A candle trades at or through X (any wick) |
| **closes above / below** X | A 30m candle closes on that side of X |
| **rejects** X | Touches X and closes back on the side it came from, with a real wick (wick ≥ 30% of the candle) or a pattern |
| **rejects X with a pattern** | Same, and the candle is a hammer, shooting star or engulfing |
| **holds** X | Rejects X **in the direction of the trend** (support in an uptrend, resistance in a downtrend) |
| **accepts above / below** X | Closes beyond X (the level gave way) |
| **reclaims** X | Was below X, then a candle closes back above it |
| **loses** X | Was above X, then a candle closes back below it |
| **breaks above / below** X | Closes beyond X **from the other side, with momentum**: the candle body is at least half the 30m ATR |
| **retests** X | After a break, comes back to touch X |
| **fails at** X | Breaks X, then closes back inside within 2 candles (a trap) |

## 3. Words for conditions

| Say | Exact meaning |
|---|---|
| **trending up / down** | The last 3+ candles closed above (below) both the EMA line and the BB basis |
| **choppy** | Price has crossed the lines 2+ times in the last 6 candles |
| **stretched** | Price is at least 1.5× the 30m ATR away from the BB basis |
| **the day is up / down N%** | Price has moved N% of the daily ATR from today's open |
| **above / below** X | The last 30m close is above / below X |
| **in Asia / London / NY** | Only during that session (Asia 18:00-03:00, London 03:00-09:30, NY 09:30-17:00) |
| **until 3am**, **until NY open** | The scenario expires then |

## 4. The scenarios

Each one gives the trigger, entry, stop and target the desk will use. Anything
in **[brackets]** is a default you can change by saying so.

### A. Fade a stretched level ("fade")
> "Short a rejection at VAH **if the day is up 50%**."
> "Fade ATR+75% if stretched."

- **Trigger:** rejects the level with a pattern, while stretched **and** the day has moved [50%] of its ATR toward that level
- **Entry:** the rejection candle's close
- **Stop:** beyond the wick [+2 pts]
- **Target:** [the first level at least 1.5R away] back toward value
- **Evidence:** the strongest setup in testing (8 out-of-sample trades, 62% win rate). NY was strongest.

### B. Reclaim and rotate ("reclaim")
> "Price is above VAL. In Asia, if it **reclaims VAL**, go long to VAH."

- **Trigger:** dips below the level, then a candle closes back above it (mirror for shorts: **loses**)
- **Entry:** the reclaiming candle's close
- **Stop:** below the lowest point of the dip [−2 pts]
- **Target:** the level you name, else [the next level at least 1.5R away]
- **Evidence:** not tested yet

### C. Hold in a trend ("pullback")
> "Trending up. Buy if it **holds** POC."

- **Trigger:** trending in that direction, then rejects the level with the trend [with a pattern]
- **Entry:** the rejection candle's close
- **Stop:** beyond the wick [+2 pts]
- **Target:** the level you name, else [the next level at least 1.5R away]
- **Evidence:** mixed in testing (helped 10/6, hurt 9/25)

### D. Breakout and go ("break")
> "If it **breaks above** VAH, go to ATR+50%."

- **Trigger:** closes beyond the level from the other side with momentum (body at least half the 30m ATR)
- **Entry:** the breakout candle's close
- **Stop:** back inside the level, past the candle's midpoint [−2 pts]
- **Target:** the level you name, else [the next level at least 1.5R away]
- **Evidence:** not tested yet. Fast breakouts often have wide stops, so expect 1 MNQ or a skip.

### E. Break and retest ("retest")
> "If it breaks above VAH, buy the **retest** of VAH."

- **Trigger:** breaks the level, then within [6] candles comes back, touches it, and the candle closes back on the breakout side
- **Entry:** the retest candle's close
- **Stop:** beyond the retest candle's wick [+2 pts]
- **Target:** the level you name, else [the next level at least 1.5R away]
- **Evidence:** not tested yet

### F. Failed breakout ("trap")
> "If it breaks above VAH and **fails**, short back to POC."

- **Trigger:** breaks the level, then closes back inside within [2] candles
- **Entry:** the close back inside
- **Stop:** beyond the high (low) of the failed break [+2 pts]
- **Target:** the level you name, else [POC / the next level at least 1.5R away]
- **Evidence:** not tested yet

### G. Trend turn ("trend cross")
> "In Asia, go with the **trend turn**."

- **Trigger:** closes beyond both the EMA line and the BB basis after 3+ candles on the other side, or with a breakout candle (body ≥ 1.5 ATR)
- **Entry:** that candle's close
- **Stop:** beyond the farther line [+2 pts]
- **Target:** the first level at least 1.5R away
- **Evidence:** weak in London/NY (19-20% win rate out of sample); around break-even in Asia. Use with care.

## 5. Modifiers you can add to any scenario

| Say | Effect |
|---|---|
| `target X` | Use X as the target |
| `stop below / above X` | Use that stop instead of the default |
| `size 2` | Fixed contracts (still capped at $250 risk) |
| `wick ok` | Accept wick rejections, not just patterns |
| `pattern only` | Require a hammer / shooting star / engulfing |
| `only if the day is up/down N%` | Add a day-range condition |
| `only if stretched` | Add the stretch condition |
| `once` | Disarm after it triggers once (the default) |
| `until <time>` | Expiry |
| `paper` / `live` | Simulate, or place real orders (live must be enabled separately) |

## 6. Rules that always apply

- Max **$250 risk** per trade, 1-3 MNQ; skipped if the target is under **1.5R**
- Max **2 trades per session**, one position at a time
- No new entries **15:30-18:00**
- Daily stop at **-$1,000**
- The desk never trades a scenario you haven't armed

## 7. Structured format (for ChatGPT or any other tool)

Scenarios can also be given as JSON. The desk checks every field and reads the
scenario back in plain English before it can be armed.

```json
{
  "name": "Asia reclaim VAL to VAH",
  "type": "reclaim",
  "side": "long",
  "level": "pdVAL",
  "conditions": [{"price": "above", "level": "pdVAL"}],
  "session": "Asia",
  "until": "03:00",
  "target": "pdVAH",
  "stop": "default",
  "size": null,
  "rejection": "pattern",
  "once": true,
  "mode": "paper"
}
```

| Field | Values |
|---|---|
| `name` | Any short name |
| `type` | `fade`, `reclaim`, `lose`, `pullback`, `breakout`, `retest`, `trap`, `trend_turn` |
| `side` | `long` or `short` |
| `level` | A level word from section 1 (`pdVAH`, `VAH 9/22`, `ATR+50%`, `open`, `weekly high`) or a price. Not used by `trend_turn` |
| `conditions` | List, any of: `{"price": "above"/"below", "level": <level>}`, `{"trend": "up"/"down"}`, `{"stretched": true}`, `{"day_range_pct": 50}`, `{"not_choppy": true}`. `day_range_pct` means the day has moved at least that % of its ATR from the open: **against** the trade for `fade` (up before a short fade, down before a long fade), **with** the trade for every other type |
| `session` | `Asia`, `London`, `NY` or `any` |
| `until` | `"HH:MM"` ET expiry, or `null` for end of session |
| `target` | A level, a price, or `"default"` (first level at least 1.5R away) |
| `stop` | `"default"` (the scenario's stop rule) or `{"beyond": <level or price>}` |
| `size` | Contracts (1-3), or `null` for automatic sizing |
| `rejection` | `pattern` or `wick` (for `fade`, `pullback`, `retest`) |
| `once` | `true` to disarm after one trigger |
| `mode` | `paper` or `live` |

Types map to section 4: `fade` = A, `reclaim` / `lose` = B, `pullback` = C,
`breakout` = D, `retest` = E, `trap` = F, `trend_turn` = G.

## Example

> **You:** "Price is above VAL. In Asia, if it reclaims VAL, go long to VAH."
>
> **Desk:** Scenario B (reclaim), long.
> In Asia until 03:00. Condition: last close above pdVAL 30812.
> Trigger: a candle trades below 30812, then a 30m candle closes back above it.
> Entry at that close; stop 2 pts below the dip's low; target pdVAH 31018.
> 1-3 MNQ, max $250, skip under 1.5R. Paper. **Arm it?**
