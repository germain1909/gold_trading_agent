Plan for Tuesday 10/13 (NQ, December contract NQZ6). The session opens Monday 18:00 ET.
Use the scenario vocabulary and JSON format from my instructions above.

## Multi-timeframe read (data as of Monday 10/12, ~16:10 ET; RSI 14, MACD 12/26/9, EMA 20/50)

**Monthly** (contract history only since April, so indicators aren't meaningful yet):
- Uptrend from the July low (27,494) to a contract high of 31,616.5 on 10/6. October so far: 30,529-31,616.

**Weekly:**
- Last week (10/5-10/9): high 31,616.5, low 30,792, close 31,123.
- RSI 69 (strong, near overbought). MACD rising. Price far above the weekly EMA20 (~29,808).

**Daily:**
- Monday 10/12: open 31,145.5, high 31,289, low 30,844, ~30,890 at 16:10 (down ~57% of daily ATR).
- RSI slid from 69 (10/6) to 55. MACD 340 vs signal 338: the histogram is +1.7 and falling, about to cross bearish.
- Still above EMA20 (30,740) and EMA50 (30,255). Daily ATR(14) ≈ 452.

**4H:** short-term downtrend. Below EMA20 (~31,106) and EMA50 (~31,063). RSI ~40. MACD below its signal with the histogram negative and growing.

**1H:** oversold into the afternoon (RSI ~29 at 14:00), now ~34. MACD histogram rising (selling slowing). Far below EMA20 (~31,006).

**15m:** MACD crossed up (histogram positive, rising), RSI ~43, a small bounce off 30,844. Below EMA50 (~30,973).

**Summary:** the higher timeframes are still up but momentum is fading (daily MACD about to cross down); the 4H is down; the 1H/15m are bouncing from oversold. Price is sitting on today's POC, just above a strong support zone.

## Levels
- Previous week high: 31,616.5
- 31,370 (≈ VAH 10/7, 31,369: two levels agree)
- Friday VAH: 31,128 (likely near ATR+50% if Tuesday opens near 30,890)
- Today's value area (tomorrow's pdVAH / pdPOC / pdVAL), provisional until the 17:00 close: VAH ~31,028, POC ~30,891.5, VAL ~30,848
- Previous week low: 30,792
- Daily EMA20: ~30,740 (just under the weekly low: support zone 30,740-30,792)
- ATR lines from Tuesday's open are computed by the desk

## Evidence from my backtests (30m candles)
- Breakouts have been the strongest generic scenario (44% win, 34 trades).
- Traps and fades have done well in small samples.
- "Lose pdVAH" has been weak (20% win) - avoid it.

## Scenario ideas to turn into the plan
1. Break below the previous week low (30,792) with a strong candle → short, default target.
2. Trap below 30,792 (failed break of the weekly low) → long to pdPOC.
3. Reclaim pdVAL → long to pdVAH.
4. Break above pdVAH → long to Friday VAH (31,128).
5. Fade at Friday VAH (31,128) if the day is up 50%+ → short to pdPOC.
6. Fade 31,370 on a strong rally → short to Friday VAH.

## What I need from you
- The plan in plain English (context, levels, bias, one if-then line per scenario, no-trade conditions).
- The scenarios as one JSON array, all `"mode": "paper"` (I'll switch the ones I want to live).
- Use `pdVAH` / `pdPOC` / `pdVAL` for today's value area (the desk fills in the final prices), and plain prices for the other levels (e.g. `30792`, `31128`, `31370`).
- Point out any idea that conflicts with the multi-timeframe read, and ask me if anything is ambiguous.
