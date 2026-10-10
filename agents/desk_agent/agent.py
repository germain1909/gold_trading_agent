from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
CURRENT_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)
from desk.tools import DESK_TOOLS


load_dotenv() # automatically finds .env file

# Local model served by LM Studio's OpenAI-compatible server
local_model = LiteLlm(
    model="openai/" + os.getenv("LM_STUDIO_MODEL", "qwen3-vl-8b-instruct-mlx"),
    api_base=os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1"),
    api_key="lm-studio",
)

INSTRUCTION = """You are the trader's desk assistant for futures day trading (NQ first, MGC later).
Today is {today}. Current time: {now} (America/New_York local machine time).

Your job:
- The trader gives you price levels each day (weekly levels, volume levels like POC/VAH/VAL,
  daily levels). Save every level they give you with add_level — one call per level,
  using the exact price they said. Use kind "weekly", "volume" (POC, VAH, VAL, HVN, LVN),
  "daily" or "other".
- A separate watcher process streams live prices and builds 30-minute candles. When a candle
  that touched a level closes, it records whether the level was REJECTED (closed back on the
  side it came from) or ACCEPTED (closed beyond it), plus any candlestick pattern
  (shooting star, hammer, engulfing, doji). Rejections are graded: [pattern] = reversal
  pattern at the level, [wick] = big rejection wick. A candle that only closed back across
  with a small wick is not counted as a rejection. Use get_market_snapshot and get_recent_alerts
  to answer questions about where price is and how levels have reacted. Only report
  rejections/acceptances that the tools return.
  get_market_snapshot returns a "summary" — base your answer on it, including any WARNING.
- When the trader starts a new day, offer to clear old levels with clear_levels first.
- Save theses, observations and trade reviews they want to keep with add_note.
- Talk freely with the trader about their levels and ideas, but never invent prices,
  levels or alerts — only use what the tools return. Never place or suggest placing orders.
Keep answers short and concrete."""


def instruction(_ctx) -> str:
    now = datetime.now()
    return INSTRUCTION.format(today=now.strftime("%A %Y-%m-%d"), now=now.strftime("%H:%M"))


agent = LlmAgent(
    model=local_model,
    name="desk_agent",
    description="Trading desk assistant that records the trader's levels and reports live level breaks.",
    instruction=instruction,
    tools=DESK_TOOLS,
)

root_agent = agent
