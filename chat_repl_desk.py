import asyncio
import datetime

from agents.desk_agent.agent import root_agent
from desk import store
from desk.tools import describe_alert

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types  # Content / Part for messages

# ANSI color codes
CYAN = "\033[96m"
YELLOW = "\033[93m"
RESET = "\033[0m"
BOLD = "\033[1m"

# --- ADK setup ---

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
                text_parts = [p.text for p in content.parts if getattr(p, "text", None)]
                if text_parts:
                    last_text = "".join(text_parts)

    if last_text is None:
        last_text = "[No final response from agent]"

    return last_text


def print_new_alerts(seen_id: int) -> int:
    """Show any level rejections the watcher recorded since the last prompt (acceptances are only recorded)."""
    alerts = [a for a in reversed(store.recent_alerts(limit=20)) if a["id"] > seen_id]
    for a in alerts:
        if a["outcome"] == "rejected":
            print(f"{YELLOW}{BOLD}⚡ {a['symbol']} {describe_alert(a)}{RESET}")
    return max([seen_id] + [a["id"] for a in alerts])


async def main():
    print(f"\n{CYAN}{BOLD}📈 Trading Desk Online 📈{RESET}")
    print("Give me your levels, ask what's broken, or save notes.")
    print("Run `python -m desk.watcher NQ` in another terminal for live monitoring.")
    print("Type 'quit' or 'exit' to leave.\n")

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
        session_id=SESSION_ID,
    )

    latest = store.recent_alerts(limit=1)
    seen_alert_id = latest[0]["id"] if latest else 0

    while True:
        try:
            user = input("You: ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting…")
            break

        seen_alert_id = print_new_alerts(seen_alert_id)

        if user.strip().lower() in {"quit", "exit"}:
            print("Exiting…")
            break

        if not user.strip():
            continue  # ignore empty input

        print("\n[Thinking…]\n")

        response = await call_agent(user)

        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"{CYAN}Desk [{timestamp}]: {response}{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
