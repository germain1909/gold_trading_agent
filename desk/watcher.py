"""
Live level watcher. Streams trades from the TopstepX SignalR market hub,
builds candles (30-minute by default), and when a candle closes checks every
watched level it touched:

  - REJECTED: the candle reached the level and closed back on the side it
    opened on (e.g. traded up into resistance, closed below it)
  - ACCEPTED: the candle closed beyond the level

Only rejections alert (terminal + macOS popup), naming any candlestick pattern
on the closing candle (shooting star, hammer, engulfing, doji). Acceptances are
recorded quietly so the agent knows a level flipped. No LLM is involved here.

Run from the project root:
    python -m desk.watcher NQ
    python -m desk.watcher NQ MGC
    python -m desk.watcher NQ --plan plans/2026-10-12.json   # also run a day's scenarios (paper + Slack)
"""
import logging
import re
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

from signalrcore.hub_connection_builder import HubConnectionBuilder

from topstep.client import TopstepClient
from . import store
from .patterns import evaluate_level, STRENGTHS

MARKET_HUB = os.getenv("TOPSTEP_MARKET_HUB", "wss://rtc.topstepx.com/hubs/market")
CANDLE_MINUTES = int(os.getenv("DESK_CANDLE_MINUTES", "30"))
# Weakest rejection grade that alerts: "wick" (default), "pattern", or "weak" (everything).
# A weak close-back (small wick, e.g. a momentum candle that opened next to the level)
# is not treated as a rejection by default.
MIN_STRENGTH = os.getenv("DESK_MIN_STRENGTH", "wick").lower()
if MIN_STRENGTH not in STRENGTHS:
    raise SystemExit(f"DESK_MIN_STRENGTH must be one of {STRENGTHS}")
CANDLE_SECONDS = CANDLE_MINUTES * 60
CLOSE_GRACE_SECONDS = 3         # wait this long past the bucket end for late trades
LOOP_SECONDS = 2
STALE_STREAM_SECONDS = 300      # rebuild the connection if nothing arrives for this long
TOKEN_MAX_AGE_SECONDS = 20 * 3600

RED = "\033[91m"
GREEN = "\033[92m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def resolve_front_month(client: TopstepClient, symbol: str) -> Dict[str, Any]:
    """
    Find the active contract for a root symbol. Topstep's search for "NQ" also
    returns MNQ and other products, so match the contract name exactly:
    root + month code + year digit (e.g. NQZ6).
    """
    symbol = symbol.upper()
    for c in client.search_contracts(symbol):
        name = c.get("name", "")
        if name.startswith(symbol) and len(name) == len(symbol) + 2 and c.get("activeContract", True):
            return c
    raise RuntimeError(f"No active contract found for symbol '{symbol}'")


def parse_ts(value: Optional[str]) -> float:
    """Parse a Topstep ISO timestamp (may have 7 fractional digits) to epoch seconds."""
    if not value:
        return time.time()
    try:
        head, _, tail = value.partition(".")
        if tail:
            frac = "".join(ch for ch in tail if ch.isdigit())
            tz = tail[len(frac):]
            value = f"{head}.{frac[:6]}{tz}"
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def bucket_start(ts: float) -> int:
    return int(ts // CANDLE_SECONDS * CANDLE_SECONDS)


def local_hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def px(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".")


def notify(title: str, message: str) -> None:
    """Terminal bell plus a macOS notification (ignored on other systems)."""
    print("\a", end="", flush=True)
    if sys.platform == "darwin":
        script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class LevelWatcher:
    def __init__(self, symbols: List[str], plan_path: Optional[str] = None):
        self.client = TopstepClient()
        self.contracts: Dict[str, Dict[str, Any]] = {}   # contract_id -> {symbol, name, tick_size}
        for sym in symbols:
            c = resolve_front_month(self.client, sym)
            self.contracts[c["id"]] = {"symbol": sym.upper(), "name": c["name"], "tick_size": c.get("tickSize")}

        self.lock = threading.Lock()
        self.levels: Dict[int, Dict[str, Any]] = {}      # level_id -> level row
        self.candle: Dict[str, Dict[str, Any]] = {}      # symbol -> candle being built
        self.prev_candle: Dict[str, Dict[str, Any]] = {} # symbol -> last closed candle
        self.latest: Dict[str, tuple] = {}               # symbol -> (contract_id, price), flushed to DB by run()
        # Candles starting before this time were only partly seen, so their open is
        # unreliable; they're skipped. Backfill moves it back to the current bucket.
        first_full = bucket_start(time.time()) + CANDLE_SECONDS
        self.complete_from: Dict[str, int] = {c["symbol"]: first_full for c in self.contracts.values()}

        self.hub = None
        self.connected = False
        self.last_message_at = 0.0
        self.token_at = 0.0

        # Plan mode: run a day's scenarios through the trigger detector (desk.plan_runner)
        self.plan_path = plan_path
        self.plan = None
        self.plan_symbol = symbols[0].upper()
        self.runner = None
        self.plan_done = False
        self.plan_lock = threading.Lock()
        self.history: List[Dict[str, Any]] = []          # closed 30m candles for the plan symbol, oldest first
        self.notifier = None
        self.broker = None
        self.last_live_poll = 0.0
        if plan_path:
            from .scenarios import load
            from .slack_notifier import SlackNotifier
            from .broker import LiveBroker
            self.plan = load(open(plan_path).read())
            self.notifier = SlackNotifier()
            self.broker = LiveBroker(self.client, self.notifier)      # mode from LIVE_TRADING (default off)
            print(f"{BOLD}{RED if self.broker.live else ''}Broker: {self.broker.describe()}{RESET}")
            if self.broker.live and self.broker.any_position():
                print(f"{RED}{BOLD}The account already has an open position - live entries are blocked "
                      f"until it is flat{RESET}")

    # ---------- levels ----------

    def refresh_levels(self) -> None:
        watched = {c["symbol"] for c in self.contracts.values()}
        fresh = {lv["id"]: lv for lv in store.list_levels() if lv["symbol"] in watched}
        with self.lock:
            added = fresh.keys() - self.levels.keys()
            removed = self.levels.keys() - fresh.keys()
            self.levels = fresh
        for level_id in added:
            lv = fresh[level_id]
            print(f"{DIM}+ watching {lv['symbol']} {px(lv['price'])} {lv['label']}{RESET}")
        for level_id in removed:
            print(f"{DIM}- stopped watching level #{level_id}{RESET}")

    # ---------- candles ----------

    def backfill(self) -> None:
        """Seed the forming candle and the previous candle from Topstep history."""
        now_bucket = bucket_start(time.time())
        for contract_id, info in self.contracts.items():
            try:
                bars = self.client.get_recent_bars(contract_id, CANDLE_MINUTES, lookback_minutes=CANDLE_MINUTES * 6)
            except Exception as e:
                print(f"{DIM}Backfill failed for {info['symbol']} ({e}); first candle will be skipped{RESET}")
                continue
            candles = [
                {"start": bucket_start(parse_ts(b["t"])), "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b.get("v", 0)}
                for b in bars
            ]
            sym = info["symbol"]
            with self.lock:
                self.complete_from[sym] = now_bucket
                if candles and candles[-1]["start"] == now_bucket:
                    self.candle[sym] = candles.pop()
                if candles:
                    self.prev_candle[sym] = candles[-1]
            forming = self.candle.get(sym)
            if forming:
                print(f"{DIM}{sym}: resumed {CANDLE_MINUTES}m candle from {local_hm(forming['start'])} "
                      f"(O {px(forming['o'])} H {px(forming['h'])} L {px(forming['l'])}){RESET}")

    def add_trade(self, symbol: str, price: float, volume: float, ts: float) -> None:
        start = bucket_start(ts)
        closed = None
        with self.lock:
            cur = self.candle.get(symbol)
            if cur is None or start > cur["start"]:
                closed = cur
                self.candle[symbol] = {"start": start, "o": price, "h": price, "l": price, "c": price, "v": volume,
                                       "partial": start < self.complete_from[symbol]}
            elif start == cur["start"]:
                cur["h"] = max(cur["h"], price)
                cur["l"] = min(cur["l"], price)
                cur["c"] = price
                cur["v"] += volume
            # trades for an already-closed bucket are ignored
        if closed:
            self.close_candle(symbol, closed)

    def close_due_candles(self) -> None:
        """Close candles whose time is up even if no new trade has arrived."""
        now = time.time()
        due = []
        with self.lock:
            for symbol, cur in list(self.candle.items()):
                if now >= cur["start"] + CANDLE_SECONDS + CLOSE_GRACE_SECONDS:
                    due.append((symbol, self.candle.pop(symbol)))
        for symbol, cur in due:
            self.close_candle(symbol, cur)

    def close_candle(self, symbol: str, candle: Dict[str, Any]) -> None:
        with self.lock:
            prev = self.prev_candle.get(symbol)
            self.prev_candle[symbol] = candle
            levels = [lv for lv in self.levels.values() if lv["symbol"] == symbol]
        if candle.get("partial"):
            print(f"{DIM}{symbol}: skipped partial {CANDLE_MINUTES}m candle from {local_hm(candle['start'])}{RESET}")
            return
        print(f"{DIM}{symbol} {CANDLE_MINUTES}m {local_hm(candle['start'])} closed: "
              f"O {px(candle['o'])} H {px(candle['h'])} L {px(candle['l'])} C {px(candle['c'])}{RESET}")
        if self.plan and symbol == self.plan_symbol:
            self.plan_candle_closed(candle)
        for lv in levels:
            result = evaluate_level(lv["price"], candle, prev)
            if result is None:
                continue
            if result["outcome"] == "rejected" and STRENGTHS.index(result["strength"]) < STRENGTHS.index(MIN_STRENGTH):
                print(f"{DIM}{symbol} closed back across {px(lv['price'])} with a {result['strength']} wick "
                      f"- not a rejection (DESK_MIN_STRENGTH={MIN_STRENGTH}){RESET}")
                continue
            self.alert(lv, candle, result)

    def alert(self, lv: Dict[str, Any], candle: Dict[str, Any], result: Dict[str, Any]) -> None:
        pattern = ", ".join(result["patterns"])
        candle_info = {k: candle[k] for k in ("start", "o", "h", "l", "c", "v")}
        store.add_alert(lv, result["outcome"], result["bias"], candle["c"], pattern, f"{CANDLE_MINUTES}m", candle_info,
                        result["strength"])

        label = f" ({lv['label']})" if lv["label"] else ""
        stamp = datetime.now().strftime("%H:%M:%S")
        if result["outcome"] == "accepted":
            # Recorded so the agent knows the level flipped, but no popup:
            # acceptances are mostly noise when price chops around a level.
            side = "above" if result["bias"] == "bullish" else "below"
            print(f"{DIM}[{stamp}] {lv['symbol']} accepted {side} {px(lv['price'])}{label} - "
                  f"{CANDLE_MINUTES}m close {px(candle['c'])}{RESET}")
            return
        msg = (f"{lv['symbol']} REJECTED [{result['strength']}] {px(lv['price'])}{label} as {result['role']} - "
               f"{result['bias']}, {pattern or 'no pattern'} - {CANDLE_MINUTES}m close {px(candle['c'])}")
        color = GREEN if result["bias"] == "bullish" else RED
        print(f"{color}{BOLD}[{stamp}] {msg}{RESET}")
        notify("Level rejected", msg)

    # ---------- stream handlers ----------

    def on_trade(self, args) -> None:
        contract_id, data = args[0], args[1]
        info = self.contracts.get(contract_id)
        if info is None:
            return
        self.last_message_at = time.time()
        trades = data if isinstance(data, list) else [data]
        for t in trades:
            price = t.get("price")
            if price is None:
                continue
            self.latest[info["symbol"]] = (contract_id, float(price))
            self.add_trade(info["symbol"], float(price), float(t.get("volume") or 0), parse_ts(t.get("timestamp")))
            if self.runner and info["symbol"] == self.plan_symbol:
                with self.plan_lock:
                    self.runner.on_price(float(price))

    def on_quote(self, args) -> None:
        contract_id, data = args[0], args[1]
        info = self.contracts.get(contract_id)
        if info is None:
            return
        self.last_message_at = time.time()
        if data.get("lastPrice") is not None:
            self.latest[info["symbol"]] = (contract_id, float(data["lastPrice"]))

    def flush_prices(self) -> None:
        for symbol, (contract_id, price) in list(self.latest.items()):
            store.set_price(symbol, contract_id, price)

    # ---------- plan mode ----------

    def load_history(self) -> None:
        """Closed 30m candles for the last few days, so indicators are warmed up."""
        from .replay import fetch_bars
        from datetime import timezone
        cid = next(cid for cid, info in self.contracts.items() if info["symbol"] == self.plan_symbol)
        now = datetime.now(timezone.utc)
        bars = fetch_bars(self.client, cid, now - timedelta(days=6), now)
        current = bucket_start(time.time())
        self.history = [{"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} for b in bars
                        if bucket_start(parse_ts(b["t"])) < current]
        print(f"{DIM}Plan: loaded {len(self.history)} closed {CANDLE_MINUTES}m candles for indicators{RESET}")

    def plan_context(self):
        from .detector import Context
        from .indicators import ema_line, bb_basis, atr
        closes = [c["c"] for c in self.history]
        r = self.runner
        return Context(self.history, ema_line(closes), bb_basis(closes), atr(self.history), r.levels,
                       r.day_open, r.day_atr, CANDLE_MINUTES, r.cfg)

    def ensure_runner(self, live_last: bool) -> None:
        """Arm the plan once the session has a closed candle. Earlier candles only warm up scenario memory."""
        if self.runner or self.plan_done or not self.plan:
            return
        from datetime import timezone
        from .plan_runner import PlanRunner, trading_day_for, build_day_levels
        from .profile import session_window
        d = trading_day_for(datetime.now(timezone.utc))
        start, _ = session_window(d, "full")
        today = [k for k, c in enumerate(self.history) if datetime.fromisoformat(c["t"]) >= start]
        if not today:
            return
        day_open = self.history[today[0]]["o"]
        print(f"{DIM}Plan: computing levels for {d}...{RESET}")
        levels, cid, day_atr = build_day_levels(self.client, self.plan_symbol, d, day_open)
        watched = next(c for c, info in self.contracts.items() if info["symbol"] == self.plan_symbol)
        if cid != watched:
            print(f"{RED}Plan: levels are from {cid} but the watcher streams {watched} - check the contract roll{RESET}")
        name = next(info["name"] for info in self.contracts.values() if info["symbol"] == self.plan_symbol)
        self.runner = PlanRunner(self.plan, levels, day_open, day_atr, self.plan_symbol, name, d,
                                 notifier=self.notifier, broker=self.broker)
        if self.broker:
            self.broker.session_start = start            # the live daily loss stop counts fills from here
        m = re.search(r"\d{4}-\d{2}-\d{2}", self.plan_path or "")
        if m and m.group(0) != d.isoformat():
            warn = f"plan file is for {m.group(0)} but today's session is {d}"
            print(f"{RED}{BOLD}Plan: {warn}{RESET}")
            self.runner.notifier.send_message(f":warning: {warn}")
        ctx = self.plan_context()
        warm = today[:-1] if live_last else today
        for k in warm:
            self.runner.on_candle(ctx, k, warmup=True)
        if warm:
            print(f"{DIM}Plan: replayed {len(warm)} earlier candle(s) today to rebuild scenario memory "
                  f"(no trades from them){RESET}")

    def plan_candle_closed(self, candle: Dict[str, Any]) -> None:
        from datetime import timezone
        t = datetime.fromtimestamp(candle["start"], tz=timezone.utc).isoformat()
        with self.plan_lock:
            if self.history and self.history[-1]["t"] >= t:
                return
            self.history.append({"t": t, "o": candle["o"], "h": candle["h"], "l": candle["l"], "c": candle["c"]})
            if self.runner is None:
                self.ensure_runner(live_last=True)
            if self.runner and not self.runner.ended:
                self.runner.on_candle(self.plan_context(), len(self.history) - 1)

    def check_live(self) -> None:
        """Every 5 seconds while a live position is open: has Topstep closed it? Flatten at 16:55 ET."""
        if not self.runner or self.runner.ended or not self.runner.position or not self.runner.position.get("live"):
            return
        if time.time() - self.last_live_poll < 5:
            return
        self.last_live_poll = time.time()
        from .replay import ET
        now = datetime.now(ET)
        with self.plan_lock:
            if (now.hour, now.minute) >= (16, 55) and now.hour < 18:
                self.broker.flatten("16:55 ET - flat before the close")
            self.runner.poll_live()

    def check_end_of_day(self) -> None:
        """Wrap up the plan at the 17:00 ET close."""
        if not self.runner or self.runner.ended:
            return
        from .replay import ET
        if 17 <= datetime.now(ET).hour < 18:
            with self.plan_lock:
                self.runner.end_of_day()
            self.plan_done = True
            try:
                from .journal import compute_outcomes
                n = compute_outcomes(self.runner.day.isoformat(), self.plan_symbol, self.client)
                print(f"{DIM}Journal: computed {n} shadow outcome(s) for {self.runner.day}{RESET}")
            except Exception as e:
                print(f"{RED}Journal: shadow outcomes failed: {e}{RESET}")

    # ---------- connection ----------

    def subscribe(self) -> None:
        self.connected = True
        self.last_message_at = time.time()
        for contract_id, info in self.contracts.items():
            self.hub.send("SubscribeContractQuotes", [contract_id])
            self.hub.send("SubscribeContractTrades", [contract_id])
            print(f"Subscribed to {info['symbol']} ({info['name']}, {contract_id})")

    def on_close(self) -> None:
        self.connected = False
        print(f"{DIM}Market hub connection closed{RESET}")

    def connect(self) -> None:
        if self.hub is not None:
            try:
                self.hub.stop()
            except Exception:
                pass
        if time.time() - self.token_at > TOKEN_MAX_AGE_SECONDS:
            self.client.login_with_key()
            self.token_at = time.time()
        token = self.client._token
        self.hub = (
            HubConnectionBuilder()
            .with_url(
                f"{MARKET_HUB}?access_token={token}",
                options={"skip_negotiation": True, "access_token_factory": lambda: token},
            )
            .build()
        )
        self.hub.on_open(self.subscribe)
        self.hub.on_close(self.on_close)
        self.hub.on_error(lambda e: print(f"{RED}Market hub error: {e}{RESET}"))
        self.hub.on("GatewayTrade", self.on_trade)
        self.hub.on("GatewayQuote", self.on_quote)
        self.hub.start()

    def run(self) -> None:
        self.refresh_levels()
        self.connect()
        self.backfill()
        if self.plan:
            self.load_history()
            with self.plan_lock:
                self.ensure_runner(live_last=False)
            if not self.runner:
                print(f"{DIM}Plan: waiting for the session's first candle to close before arming{RESET}")
        try:
            while True:
                time.sleep(LOOP_SECONDS)
                self.refresh_levels()
                self.close_due_candles()
                self.flush_prices()
                self.check_live()
                self.check_end_of_day()
                stale = time.time() - self.last_message_at > STALE_STREAM_SECONDS
                token_old = time.time() - self.token_at > TOKEN_MAX_AGE_SECONDS
                if not self.connected or stale or token_old:
                    print(f"{DIM}Reconnecting to market hub...{RESET}")
                    time.sleep(3)
                    self.connect()
        except KeyboardInterrupt:
            print("\nStopping watcher…")
            self.hub.stop()


def main() -> None:
    logging.getLogger("SignalRCoreClient").setLevel(logging.WARNING)
    args = sys.argv[1:]
    plan_path = None
    if "--plan" in args:
        k = args.index("--plan")
        if k + 1 >= len(args):
            raise SystemExit("--plan needs a file, e.g. --plan plans/2026-10-12.json")
        plan_path = args[k + 1]
        args = args[:k] + args[k + 2:]
    symbols = args or ["NQ"]
    print(f"{BOLD}Level watcher starting for {', '.join(s.upper() for s in symbols)}{RESET}")
    print(f"{DIM}DB: {store.DB_PATH} | {CANDLE_MINUTES}m candles | "
          f"min rejection strength: {MIN_STRENGTH}"
          f"{f' | plan: {plan_path} (paper)' if plan_path else ''}{RESET}")
    LevelWatcher(symbols, plan_path).run()


if __name__ == "__main__":
    main()
