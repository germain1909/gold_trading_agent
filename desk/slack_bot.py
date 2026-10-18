"""
Slack bot for the trading desk (Socket Mode - no public URL needed). Runs inside the watcher.

Messages from the trader's Slack user (SLACK_USER_ID) in the bot's channel or a DM are passed to
desk.slack_commands; everything else is ignored.

.env:
    SLACK_BOT_TOKEN=xoxb-...      (already used for notifications)
    SLACK_APP_TOKEN=xapp-...      (app-level token with connections:write)
    SLACK_USER_ID=U0123ABC        (the only user allowed to send commands)
"""
import logging
import re
import threading
from collections import deque

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

logger = logging.getLogger(__name__)


class SlackBot:
    def __init__(self, commands, app_token: str, bot_token: str):
        self.commands = commands
        self.seen = deque(maxlen=500)                 # Slack can redeliver events; handle each once
        self.client = SocketModeClient(app_token=app_token, web_client=WebClient(token=bot_token))
        self.client.socket_mode_request_listeners.append(self._on_request)

    def start(self) -> None:
        self.client.connect()                         # runs on background threads

    def stop(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    def _on_request(self, client: SocketModeClient, req: SocketModeRequest) -> None:
        if req.type != "events_api":
            return
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        ev = req.payload.get("event", {})
        if ev.get("type") != "message" or ev.get("bot_id") or ev.get("subtype"):
            return
        key = ev.get("client_msg_id") or ev.get("ts")
        if key in self.seen:
            return
        self.seen.append(key)
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", ev.get("text", "")).strip()
        threading.Thread(target=self.commands.handle, args=(ev.get("user"), text), daemon=True).start()
