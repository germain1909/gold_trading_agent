# Branches

What each branch adds, oldest first. Each branch builds on the one before it.

```
main
 └─ friezaTrading
     └─ friezaAndFranklin
         └─ desk-level-strategy
             └─ lock-and-roll
```

Reference days used to check every strategy change (NQ, full 23h session, 30m candles):

| Day | Levels |
|---|---|
| 2026-10-09 | 31461.75 weekly-high, 31252 VAH, 31079.25 VAL, 31020 asia-open |
| 2026-09-23 | 31018 VAH, 30944 POC, 30812 VAL, 30681 prevDayPOC |
| 2026-09-25 | 30600 VAL, 30742 POC, 30830 VAH, 30916 prevDayVAL (9/24 profile) |

---

## main

The starting point.

- `ed08866` Initial commit
- `745bb20` First agents: a question-and-answer agent and the Frieza persona agent (Google ADK + Gemini)
- README updates

## friezaTrading

- `be6f61b` **Topstep client** (`topstep/client.py`): login with API key, contract search, historical bars; first test (`tests/test_yesterday_bar.py`)
- `1e6e3a4` **Frieza CLI** (`chat_repl.py`) and the first Topstep tools for the agent

## friezaAndFranklin

Everything in friezaTrading, plus:

- `02f590a` **Franklin Saint** persona agent and CLI (`chat_repl_franklin.py`; the Frieza CLI renamed to `chat_repl_frieza.py`)
- Topstep tools: guess the front-month contract, daily bar for a date, minute bars for a CME session, save minute bars to CSV
- `TopstepContractFinder`, sample 1-minute data in `historical_data/`

Not committed: an ATR percentage-levels tool, kept in `git stash` (`stash@{0}`).

## desk-level-strategy

The trading desk: level monitoring, the strategy, risk management, and a replay/backtest tool. No LLM makes trading decisions; all rules are plain Python.

### `6d3e4c6` Trading desk, two-setup strategy, risk engine, replay

**Infrastructure**
- **Live watcher** (`desk/watcher.py`): streams NQ trades from the TopstepX SignalR market hub, builds 30m candles (backfilled on startup), and alerts (terminal + macOS popup) when a level is rejected. Acceptances are recorded without a popup.
- **Desk agent + CLI** (`agents/desk_agent`, `chat_repl_desk.py`): save levels and notes in plain English, ask where price is and how levels have reacted. Runs on a local LM Studio model.
- **Shared store** (`desk/store.py`): SQLite `data/desk.db` for levels, alerts, last prices, notes (gitignored).
- Frieza and Franklin switched from Gemini to LM Studio via LiteLLM.
- Topstep client: `search_contracts()`, `get_recent_bars()`.

**Strategy**
- **Rejections** (`desk/patterns.py`): a candle touches a level and closes back on the side it came from. Graded *pattern* (hammer, shooting star, engulfing), *wick* (wick ≥ 30% of range) or *weak*; weak is not a rejection.
- **Indicators** (`desk/indicators.py`): EMA line = EMA(15) smoothed SMA(5); BB basis = EMA(24); ATR(14).
- **Sessions** (`desk/sessions.py`): Asia 18:00–03:00, London 03:00–09:30, NY 09:30–17:00 ET.
- **Setup 1, trend cross** (`desk/setups.py`): 30m close beyond both the EMA line and BB basis. Stop beyond the lines; exit early on a close back across the EMA line.
- **Setup 2, stretched rejection**: a *pattern* rejection at a level while price is ≥ 1.5 ATR from the BB basis, trading back toward the averages. Stop beyond the wick.
- **Risk** (`desk/risk.py`): target = first level at least 1.5R away; max $250 risk per trade; max 2 trades per session; one position at a time; daily loss limit $1,000.

**Replay** (`desk/replay.py`): any past day, all three sessions, every rejection scored, and both setups simulated with 1-minute bars deciding fills.

Reference results: **10/9 +$2,074, 9/23 −$210**

### `3a005d1` Strategy v2

- **A trend cross needs a reason:** price was on the other side of both lines for 3+ candles first, **or** the cross candle is a breakout (body ≥ 1.5 ATR). Filters chop crosses through flat lines.
- **Trade smaller instead of skipping:** 1–3 MNQ (was 2–3). Max risk is still $250.

Reference results: **10/9 +$2,202, 9/23 +$282**

## lock-and-roll

Everything in desk-level-strategy, plus:

- **Lock and roll** (`desk/risk.py`, opt-in with `RISK_LOCK_ROLL=true`): when price touches the target, don't exit; lock the stop 5 points behind the target (`RISK_LOCK_BUFFER`). At that 30m candle's close:
  - closed back inside the level → exit (the level held)
  - closed beyond the level → stay in, stop behind the broken level, target the next level
- The bar that touches the target is judged by its close, since price came from the stop side.
- Replay prints each lock/roll step.

Reference results with lock and roll on:

| Day | v2 | v2 + lock and roll | Why |
|---|---|---|---|
| 10/9 | +$2,202 | +$2,142 | Both targets held; exits came 5 pts short of target |
| 9/23 | +$282 | +$548 | VAL broke on the close; the trade rode to prevDayPOC |

```bash
RISK_LOCK_ROLL=true python -m desk.replay NQ 2026-09-23 31018:VAH 30944:POC 30812:VAL 30681:prevDayPOC
```

- **Session-scoped trend-cross filter** (`desk/setups.py`, opt-in with `SETUP_CROSS_PRIOR_SESSIONS=London,NY`): apply the "3+ candles on the other side first" rule only in the listed sessions. Asia trend crosses (smoother overnight trends) don't need it; London/NY crosses (choppier) still do. Default is all sessions, the same as v2.

Added a third clean reference day: **2026-09-25** with 9/24's profile levels (30600 VAL, 30742 POC, 30830 VAH, 30916 prevDayVAL).

| Day | v2 | v2 + prior rule only in London/NY | Why |
|---|---|---|---|
| 10/9 | +$2,202 | +$2,202 | Asia winner already had 19 prior candles |
| 9/23 | +$282 | +$300 | Adds a small Asia long (+$18) |
| 9/25 | −$60 | +$640 | Asia long to VAH (+$700); London/NY chop still filtered |
| **Total** | **+$2,424** | **+$3,142** | |

```bash
SETUP_CROSS_PRIOR_SESSIONS=London,NY python -m desk.replay NQ 2026-09-25 30600:VAL 30742:POC 30830:VAH 30916:prevDayVAL
```

Note: the earlier 9/24 run used 9/24's own levels (look-ahead), so it was discarded.

### Levels from data, backtest runner, more rules

- **ATR % levels** (`desk/atr_levels.py`): the trader's TradingView indicator - trading-day open +/- 25/50/75/100% of daily ATR(14). `ATR_LEVELS=beyond` uses them only outside the value area (targets and rejection levels); `off` (default) / `all`.
- **Clues on every signal**: % of the day's ATR used, and the nearest ATR level (recorded only).
- **Volume profiles** (`desk/profile.py`): POC / VAH / VAL from 1-minute volume, full CME session, 70% value area. Matches the trader's levels to within ~5-20 pts on POC.
- **Backtest runner** (`desk/backtest.py`): any date range, levels computed automatically (prior-day profile + older VAH/POC/VAL within 1 ATR + ATR levels beyond value). Retries on Topstep rate limits; failed days are reported, never counted as $0.
- **No new entries after 15:30 ET** (`RISK_LAST_ENTRY`, on by default).
- Opt-in, under evaluation: minimum stop (`RISK_MIN_STOP_ATR` - tested, made results worse), Setup 3 trend pullback (`SETUP_PULLBACK=true`), fade only after the day moved N% of ATR (`SETUP_FADE_MIN_RANGE_PCT=50`).

Week of 10/5-10/9 with computed levels (default rules): **+$2,232**, 14 trades, worst day -$192. Mon-Thu (out of sample): +$1,066.

| 10 days (5 reference + 10/5-10/9) | Total |
|---|---|
| Default | +$5,645 |
| + trend pullback | +$5,759 |
| + fade after 50% | +$5,861 |
| + both | +$5,975 |

---

## Running

```bash
source ~/venvs/py310/bin/activate
python -m desk.replay NQ <YYYY-MM-DD> <price>:<label> ...   # backtest a day
python -m desk.watcher NQ                                    # live monitoring
python chat_repl_desk.py                                     # desk chat
python -m tests.test_patterns && python -m tests.test_risk && python -m tests.test_setups
```

Note: the live watcher alerts on level rejections only. It does not yet run the two setups or lock and roll; those exist in the replay.
