import asyncio
import datetime

from agents.frieza_agent.agent import root_agent

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types  # Content / Part for messages

# ANSI color codes
PURPLE = "\033[95m"
RESET = "\033[0m"
BOLD = "\033[1m"

# --- ADK setup ---

# IMPORTANT: app name must match what ADK infers from your project.
# Since your agent is in `agents.frieza_agent.agent`, ADK infers "agents".
APP_NAME = "agents"
USER_ID = "cli_user"
SESSION_ID = "cli_session"

session_service = InMemorySessionService()

runner = Runner(
    agent=root_agent,
    app_name=APP_NAME,
    session_service=session_service,
)


async def call_agent(prompt: str) -> str:
    """
    Call the ADK agent via Runner.run_async and return the final text reply.
    """
    # Wrap user input as a Gemini Content object
    user_content = types.Content(
        role="user",
        parts=[types.Part(text=prompt)],
    )

    last_text = None

    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=SESSION_ID,
        new_message=user_content,
    ):
        if event.is_final_response():
            content = getattr(event, "content", None)
            if content and getattr(content, "parts", None):
                # Concatenate any text parts in the final response
                text_parts = [p.text for p in content.parts if getattr(p, "text", None)]
                if text_parts:
                    last_text = "".join(text_parts)

    if last_text is None:
        last_text = "[No final response from agent]"

    return last_text


async def main():
    print(f"\n{PURPLE}{BOLD}🔥 Frieza Trading Terminal Online 🔥{RESET}")
    print("Type 'quit' or 'exit' to leave.\n")

    # ✅ Create the session once, and AWAIT it
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )

    while True:
        try:
            user = input("You: ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting…")
            break

        if user.strip().lower() in {"quit", "exit"}:
            print("Exiting…")
            break

        if not user.strip():
            continue  # ignore empty input

        print("\n[Thinking…]\n")

        response = await call_agent(user)

        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"{PURPLE}Frieza [{timestamp}]: {response}{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
