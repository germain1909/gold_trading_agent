"""
Slack notifications for the trading desk.

Fire-and-forget: a Slack problem never raises or affects trading. With no
SLACK_BOT_TOKEN, or without slack_sdk installed, every method is a no-op.

.env:
    SLACK_BOT_TOKEN=xoxb-...
    SLACK_CHANNEL=#trades
    SLACK_USER_ID=U0123456      (optional, @mentions you on entries and exits)
"""
from __future__ import annotations

import logging
import os
from typing import Optional, List

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _px(x: float) -> str:
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def _usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


class SlackNotifier:
    def __init__(self, token: Optional[str] = None, channel: Optional[str] = None) -> None:
        self._token = token or os.getenv("SLACK_BOT_TOKEN", "")
        self._channel = channel or os.getenv("SLACK_CHANNEL", "#trades")
        self._user_id = os.getenv("SLACK_USER_ID", "")
        self._client = None
        if self._token:
            try:
                from slack_sdk import WebClient
                self._client = WebClient(token=self._token)
            except ImportError:
                logger.warning("slack_sdk not installed - Slack notifications disabled")
        else:
            logger.info("SLACK_BOT_TOKEN not set - Slack notifications disabled")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def _mention(self) -> str:
        return f"<@{self._user_id}> " if self._user_id else ""

    def send_message(self, text: str) -> bool:
        """Post a message. Returns True if Slack accepted it."""
        if not self._client:
            return False
        try:
            self._client.chat_postMessage(channel=self._channel, text=text)
            return True
        except Exception as exc:  # never let Slack break trading
            logger.warning(f"Slack send_message failed: {exc}")
            return False

    # ---------- desk events ----------

    def send_session_started(self, symbol: str, contract: str, day: str, levels: List[str],
                             scenarios: List[str]) -> None:
        lines = [f":sunrise: *{symbol} plan armed for {day}* ({contract})", "*Levels:* " + " · ".join(levels),
                 f"*Watching {len(scenarios)} scenario(s):*"]
        lines += [f"  • {s}" for s in scenarios]
        self.send_message("\n".join(lines))

    def send_trade_entered(self, symbol: str, scenario: str, side: str, contracts: int, contract: str,
                           entry: float, stop: float, target: float, rr: float, risk_usd: float,
                           detail: str, mode: str = "paper") -> None:
        lines = [
            f"{self._mention()}:large_{'green' if side == 'long' else 'red'}_circle: "
            f"*[{symbol} {side.upper()}] {scenario}* ({mode.upper()})",
            f"Why:    {detail}",
            f"Entry:  {_px(entry)}  x{contracts} {contract}",
            f"Stop:   {_px(stop)}  ({_px(abs(entry - stop))} pts, -${risk_usd:,.0f})",
            f"Target: {_px(target)}  ({rr:.1f}R)",
        ]
        self.send_message("\n".join(lines))

    def send_trade_closed(self, symbol: str, scenario: str, how: str, exit_price: float,
                          pnl_pts: float, pnl_usd: float) -> None:
        icon = ":white_check_mark:" if pnl_usd > 0 else (":x:" if pnl_usd < 0 else ":heavy_minus_sign:")
        lines = [
            f"{self._mention()}{icon} *[{symbol} CLOSED] {scenario}*",
            f"Exit:   {how} at {_px(exit_price)}",
            f"P&L:    {'+' if pnl_pts >= 0 else ''}{_px(pnl_pts)} pts = {_usd(pnl_usd)}",
        ]
        self.send_message("\n".join(lines))

    def send_scenario_invalidated(self, symbol: str, scenario: str, reason: str) -> None:
        self.send_message(f":no_entry_sign: *[{symbol}] {scenario}* invalidated - {reason}")

    def send_scenario_skipped(self, symbol: str, scenario: str, detail: str, reason: str) -> None:
        self.send_message(f":warning: *[{symbol}] {scenario}* triggered ({detail}) but no trade - {reason}")

    def send_daily_limit_hit(self, reason: str, day_pnl: float) -> None:
        self.send_message("\n".join([
            ":octagonal_sign: *Daily limit reached - no more entries*",
            f"Reason:  {reason}",
            f"Day P&L: {_usd(day_pnl)}",
        ]))

    def send_daily_summary(self, symbol: str, day: str, total_pnl: float, trades: int, wins: int,
                           losses: int, unused: List[str]) -> None:
        icon = ":chart_with_upwards_trend:" if total_pnl >= 0 else ":chart_with_downwards_trend:"
        lines = [
            f"{icon} *{symbol} daily summary - {day}*",
            f"P&L:    {_usd(total_pnl)}",
            f"Trades: {trades}  ({wins}W / {losses}L)",
        ]
        if unused:
            lines.append("Never triggered: " + ", ".join(unused))
        self.send_message("\n".join(lines))


if __name__ == "__main__":
    # Connection check: python -m desk.slack_notifier
    n = SlackNotifier()
    if not n.enabled:
        raise SystemExit("Slack is not configured: add SLACK_BOT_TOKEN and SLACK_CHANNEL to .env "
                         "(and pip install slack_sdk).")
    ok = n.send_message(":white_check_mark: Trading desk connected - notifications will post here.")
    print("Slack message sent." if ok else "Slack rejected the message - check the token, channel name, "
                                           "and that the bot was invited to the channel.")
