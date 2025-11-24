from google.adk.agents import LlmAgent
from google.adk.tools import google_search
import os
from dotenv import load_dotenv


load_dotenv() # automatically finds .env file
# 🔐 Hard-code your API key temporarily for local testing
# os.environ["GOOGLE_API_KEY"] = ""
# os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "FALSE"
GOOGLE_GENAI_USE_VERTEXAI = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

agent = LlmAgent(
    model="gemini-2.0-flash",
    name="question_answer_agent",
    description="A helpful assistant agent that can answer questions.",
    instruction="Respond to the query using google search",
    tools=[google_search],
)

root_agent = agent