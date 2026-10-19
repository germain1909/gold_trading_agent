"""
Real orders on TopstepX (ProjectX Gateway). The only module that places orders.

Every entry is a market order with its stop-loss and take-profit attached as an
OCO bracket on Topstep's servers, so the position stays protected even if this
program dies. Requires "Auto OCO Brackets" enabled in TopstepX settings.

Live trading needs BOTH:
    LIVE_TRADING=true in .env (or `dryrun` to only print/Slack the order it would send)
    "mode": "live" on the scenario
Otherwise the plan runner paper-trades.

Hard limits (code, not just settings):
    - MNQ only, LIVE_MAX_SIZE contracts (capped at 1 here)
    - one position at a time: refuses if the account already has an MNQ position or working orders
    - its own daily loss stop (LIVE_DAILY_LOSS, default $300) from actual fills, and max trades per day
    - kill switch: the file data/KILL blocks every new entry
    - every order carries both a stop and a target; never an unprotected entry
    - flatten() cancels working orders and closes the position (used before the 17:00 close)

.env:
    LIVE_TRADING=off | dryrun | true
    TOPSTEP_ACCOUNT_ID=123456
    LIVE_DAILY_LOSS=300
    LIVE_MAX_TRADES=4
"""
import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from dotenv import load_dotenv

from .replay import post_with_retry
from .store import PROJECT_ROOT

load_dotenv()

HARD_MAX_SIZE = 1                  # absolute cap while the bot is being proven
LIVE_SYMBOL = "MNQ"
KILL_FILE = os.path.join(PROJECT_ROOT, "data", "KILL")
MIN_STOP_TICKS = 8                 # 2 points: refuse absurdly tight stops
MAX_RISK_USD = 250                 # per trade, same as the strategy's cap
MIN_RR = 1.5                       # same minimum as the strategy
# Bracket ticks are offsets from the fill, so slippage drags the stop with it (10/13: the stop
# landed exactly on the broken level). The bracket goes out with this pad on the stop, and
# correct_bracket() then moves stop and target to the planned prices once the fill is known.
STOP_PAD_TICKS = int(os.getenv("LIVE_STOP_PAD_TICKS", 4))

ORDER_MARKET, ORDER_LIMIT, ORDER_STOP = 2, 1, 4
SIDE_BUY, SIDE_SELL = 0, 1


class BrokerError(RuntimeError):
    pass


def _px(p) -> str:
    return "?" if p is None else f"{float(p):,.2f}"


class LiveBroker:
    def __init__(self, client, notifier, mode: Optional[str] = None):
        self.client = client
        self.notifier = notifier
        self.mode = (mode or os.getenv("LIVE_TRADING", "off")).lower()
        if self.mode not in ("off", "dryrun", "true"):
            raise BrokerError("LIVE_TRADING must be off, dryrun or true")
        self.daily_loss = float(os.getenv("LIVE_DAILY_LOSS", 300))
        self.max_trades = int(os.getenv("LIVE_MAX_TRADES", 4))
        self.size = min(HARD_MAX_SIZE, int(os.getenv("LIVE_MAX_SIZE", 1)))
        self.trades_today = 0
        self.last_error: Optional[str] = None         # why the last entry was refused or failed
        self.session_start: Optional[datetime] = None
        self.account_id = None
        self.contract = None
        if self.mode == "off":
            return

        acct = os.getenv("TOPSTEP_ACCOUNT_ID")
        if not acct:
            raise BrokerError("TOPSTEP_ACCOUNT_ID is not set in .env")
        self.account_id = int(acct)
        accounts = self._post("/api/Account/search", {"onlyActiveAccounts": True}).get("accounts", [])
        match = [a for a in accounts if a.get("id") == self.account_id]
        if not match:
            raise BrokerError(f"account {self.account_id} is not an active account for this API key")
        if not match[0].get("canTrade"):
            raise BrokerError(f"account {self.account_id} can't trade right now (canTrade=false)")
        self.account_name = match[0].get("name")

        from .watcher import resolve_front_month
        c = resolve_front_month(client, LIVE_SYMBOL)
        self.contract = c["id"]
        self.contract_name = c["name"]
        self.tick = float(c.get("tickSize") or 0.25)
        self.tick_value = float(c.get("tickValue") or 0.5)

    @property
    def live(self) -> bool:
        return self.mode == "true"

    @property
    def active(self) -> bool:
        return self.mode in ("dryrun", "true")

    def describe(self) -> str:
        if not self.active:
            return "live trading OFF (paper only)"
        return (f"{'LIVE' if self.live else 'DRY RUN'} on {self.account_name} ({self.account_id}), "
                f"{self.size} {self.contract_name}, daily stop -${self.daily_loss:,.0f}, max {self.max_trades} trades")

    # ---------- api ----------

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self.client._ensure_token()
        data = post_with_retry(self.client, f"{self.client._base_url}{path}", payload).json()
        if not data.get("success", False):
            raise BrokerError(f"{path} failed: {data.get('errorMessage') or data.get('errorCode')}")
        return data

    def open_position(self) -> Optional[Dict[str, Any]]:
        positions = self._post("/api/Position/searchOpen", {"accountId": self.account_id}).get("positions", [])
        mine = [p for p in positions if p.get("contractId") == self.contract and p.get("size")]
        return mine[0] if mine else None

    def any_position(self) -> List[Dict[str, Any]]:
        return [p for p in self._post("/api/Position/searchOpen", {"accountId": self.account_id}).get("positions", [])
                if p.get("size")]

    def open_orders(self) -> List[Dict[str, Any]]:
        orders = self._post("/api/Order/searchOpen", {"accountId": self.account_id}).get("orders", [])
        return [o for o in orders if o.get("contractId") == self.contract]

    def realized_since(self, start: datetime) -> float:
        """Realized P&L net of fees from actual fills on this contract since `start`."""
        trades = self._post("/api/Trade/search", {
            "accountId": self.account_id,
            "startTimestamp": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }).get("trades", [])
        total = 0.0
        for t in trades:
            if t.get("voided") or t.get("contractId") != self.contract:
                continue
            total += float(t.get("profitAndLoss") or 0) - float(t.get("fees") or 0)
        return total

    def last_fill(self, since: datetime) -> Optional[Dict[str, Any]]:
        trades = self._post("/api/Trade/search", {
            "accountId": self.account_id,
            "startTimestamp": since.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        }).get("trades", [])
        fills = [t for t in trades if not t.get("voided") and t.get("contractId") == self.contract]
        return max(fills, key=lambda t: t["creationTimestamp"]) if fills else None

    # ---------- rules ----------

    def blocked(self) -> Optional[str]:
        """Why a new live entry isn't allowed right now (None = allowed)."""
        if not self.active:
            return "live trading is off"
        if os.path.exists(KILL_FILE):
            return f"kill switch is on ({KILL_FILE})"
        if self.trades_today >= self.max_trades:
            return f"max {self.max_trades} live trades today"
        if self.live:
            others = self.any_position()
            if others:
                return "the account already has an open position: " + ", ".join(
                    f"{p['contractId']} x{p['size']}" for p in others)
            if self.open_orders():
                return f"there are working {self.contract_name} orders on the account"
            if self.session_start and self.realized_since(self.session_start) <= -self.daily_loss:
                return f"live daily loss stop (-${self.daily_loss:,.0f}) reached"
        return None

    # ---------- orders ----------

    def enter(self, side: str, ref_price: float, stop: float, target: float, tag: str) -> Optional[Dict[str, Any]]:
        """Market order with an OCO stop/target bracket. Returns the order info, or None if refused."""
        reason = self.blocked()
        if reason:
            self.last_error = f"refused: {reason}"
            self.notifier.send_message(f":no_entry: Live entry refused ({tag}): {reason}")
            return None
        stop_ticks = round(abs(ref_price - stop) / self.tick)
        target_ticks = round(abs(target - ref_price) / self.tick)
        risk_usd = stop_ticks * self.tick_value * self.size
        if stop_ticks < MIN_STOP_TICKS:
            reason = f"stop only {stop_ticks} ticks away (min {MIN_STOP_TICKS})"
        elif risk_usd > MAX_RISK_USD:
            reason = f"risk ${risk_usd:,.0f} > ${MAX_RISK_USD}"
        elif target_ticks < 1:
            reason = "target is not beyond entry"
        if reason:
            self.last_error = f"refused: {reason}"
            self.notifier.send_message(f":no_entry: Live entry refused ({tag}): {reason}")
            return None

        # Topstep wants bracket ticks signed as an offset from the fill price:
        # long -> target above (+), stop below (-); short -> target below (-), stop above (+).
        # (Unsigned ticks were rejected live: "Ticks should be less than zero when going short.")
        direction = 1 if side == "long" else -1
        sent_stop_ticks = stop_ticks + STOP_PAD_TICKS        # corrected to the planned price after the fill
        payload = {
            "accountId": self.account_id,
            "contractId": self.contract,
            "type": ORDER_MARKET,
            "side": SIDE_BUY if side == "long" else SIDE_SELL,
            "size": self.size,
            "limitPrice": None,
            "stopPrice": None,
            "trailPrice": None,
            "customTag": f"{tag[:40]}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "stopLossBracket": {"ticks": -direction * sent_stop_ticks, "type": ORDER_STOP},
            "takeProfitBracket": {"ticks": direction * target_ticks, "type": ORDER_LIMIT},
        }
        summary = (f"{side.upper()} {self.size} {self.contract_name} market, stop {stop_ticks} ticks "
                   f"(-${risk_usd:,.0f}) +{STOP_PAD_TICKS} pad until the fill, target {target_ticks} ticks")
        planned = {"side": side, "entry": ref_price, "stop": stop, "target": target}
        if not self.live:
            self.notifier.send_message(f":test_tube: DRY RUN - would send: {summary}\n```{payload}```")
            print(f"DRY RUN order: {payload}")
            self.trades_today += 1
            return {"dry": True, "payload": payload, "summary": summary, "planned": planned}
        try:
            data = self._post("/api/Order/place", payload)
        except Exception as e:
            self.last_error = f"failed: {e}"
            self.notifier.send_message(f":rotating_light: LIVE ORDER FAILED ({tag}): {e}")
            return None
        self.trades_today += 1
        return {"dry": False, "orderId": data.get("orderId"), "payload": payload, "summary": summary,
                "planned": planned, "placed_at": datetime.now(timezone.utc)}

    def _round(self, price: float) -> float:
        return round(round(price / self.tick) * self.tick, 2)

    def correct_bracket(self, order: Dict[str, Any], position: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        After the fill, move the bracket's stop and target to the planned prices.
        Returns None if the bracket orders aren't visible yet (call again), else what happened:
        {"status": "corrected" | "kept" | "failed", "fill", "stop", "target", "note"}.
        On "kept" or "failed" the padded bracket from the fill stays in place - never unprotected.
        """
        plan = order.get("planned") or {}
        long = plan.get("side") == "long"
        fill = float(position.get("averagePrice") or 0)
        orders = self.open_orders()
        stop_o = next((o for o in orders if o.get("type") == ORDER_STOP), None)
        target_o = next((o for o in orders if o.get("type") == ORDER_LIMIT), None)
        if not fill or not stop_o:
            return None
        stop, target = self._round(plan["stop"]), self._round(plan["target"])
        # slippage > 0 = filled worse than the planned entry
        out = {"fill": fill, "slippage": round((fill - plan["entry"]) * (1 if long else -1), 2),
               "padded_stop": stop_o.get("stopPrice"), "stop": stop, "target": target}

        risk_ticks = round(((fill - stop) if long else (stop - fill)) / self.tick)
        reward_ticks = round(((target - fill) if long else (fill - target)) / self.tick)
        risk_usd = risk_ticks * self.tick_value * self.size
        keep = None
        if risk_ticks < MIN_STOP_TICKS:
            keep = f"the planned stop {_px(stop)} is only {risk_ticks} ticks from the fill {_px(fill)}"
        elif risk_usd > MAX_RISK_USD:
            keep = f"from the fill {_px(fill)} the planned stop risks ${risk_usd:,.0f} > ${MAX_RISK_USD}"
        elif reward_ticks / risk_ticks < MIN_RR:
            keep = f"from the fill {_px(fill)} the planned stop/target is {reward_ticks / risk_ticks:.2f}R < {MIN_RR}R"
        if keep:
            self.notifier.send_message(f":warning: Bracket kept as filled (stop {_px(out['padded_stop'])}): {keep}")
            return {**out, "status": "kept", "note": keep}

        try:
            if stop_o.get("stopPrice") != stop:
                self._post("/api/Order/modify", {"accountId": self.account_id, "orderId": stop_o["id"],
                                                 "stopPrice": stop})
            if target_o and target_o.get("limitPrice") != target:
                self._post("/api/Order/modify", {"accountId": self.account_id, "orderId": target_o["id"],
                                                 "limitPrice": target})
            now = {o["id"]: o for o in self.open_orders()}
            got_stop = (now.get(stop_o["id"]) or {}).get("stopPrice")
            if got_stop != stop:
                raise BrokerError(f"stop reads {got_stop} after the modify")
        except Exception as e:
            note = f"{e}"
            self.notifier.send_message(f":rotating_light: Couldn't move the stop to {_px(stop)} - the padded stop "
                                       f"{_px(out['padded_stop'])} is still working. ({note})")
            return {**out, "status": "failed", "note": note}
        slip = out["slippage"]
        how = f"{abs(slip):g} pts {'worse' if slip > 0 else 'better'} than planned" if slip else "as planned"
        self.notifier.send_message(f":dart: Filled {_px(fill)} ({how}). Bracket set to the "
                                   f"plan: stop {_px(stop)}, target {_px(target)} (-${risk_usd:,.0f}).")
        return {**out, "status": "corrected", "note": ""}

    def flatten(self, reason: str) -> None:
        """Cancel working MNQ orders and close any MNQ position."""
        if not self.live:
            return
        try:
            for o in self.open_orders():
                self._post("/api/Order/cancel", {"accountId": self.account_id, "orderId": o["id"]})
            if self.open_position():
                self._post("/api/Position/closeContract", {"accountId": self.account_id, "contractId": self.contract})
                self.notifier.send_message(f":lock: Flattened {self.contract_name}: {reason}")
        except Exception as e:
            self.notifier.send_message(f":rotating_light: FLATTEN FAILED - check the account now: {e}")
