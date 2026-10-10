from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools import google_search
import os
import sys
from dotenv import load_dotenv
CURRENT_DIR = os.path.dirname(__file__)
PROJECT_ROOT = os.path.dirname(os.path.dirname(CURRENT_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)
from topstep.tools import get_yesterdays_daily_bar
from topstep.tools import guess_futures_contract
from topstep.tools import get_daily_bar_for_contract_on_date
from topstep.tools import get_minute_bars_for_cme_session
from topstep.tools import get_and_save_minute_bars_for_session


load_dotenv() # automatically finds .env file
# 🔐 Hard-code your API key temporarily for local testing
# os.environ["GOOGLE_API_KEY"] = ""
# os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
GOOGLE_GENAI_USE_VERTEXAI = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

# Local model served by LM Studio's OpenAI-compatible server
local_model = LiteLlm(
    model="openai/" + os.getenv("LM_STUDIO_MODEL", "qwen3-vl-8b-instruct-mlx"),
    api_base=os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1"),
    api_key="lm-studio",
)


agent = LlmAgent(
    model=local_model,
    name="frieza_agent",
    description="A cunning, market-dominating version of Lord Frieza who teaches elite futures day trading.""A helpful assistant agent that can answer questions.",
    instruction="You are Lord Frieza — elite futures trader (GC, CL, NQ).Mentor another version of yourself aiming for $10k/month",
    tools=[
        get_yesterdays_daily_bar,
        guess_futures_contract,
        get_daily_bar_for_contract_on_date,
         get_minute_bars_for_cme_session,
         get_and_save_minute_bars_for_session
         
    ],
)

root_agent = agent