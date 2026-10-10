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
"""
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
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
    def __init__(self, symbols: List[str]):
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
        try:
            while True:
                time.sleep(LOOP_SECONDS)
                self.refresh_levels()
                self.close_due_candles()
                self.flush_prices()
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
    symbols = sys.argv[1:] or ["NQ"]
    print(f"{BOLD}Level watcher starting for {', '.join(s.upper() for s in symbols)}{RESET}")
    print(f"{DIM}DB: {store.DB_PATH} | {CANDLE_MINUTES}m candles | "
          f"min rejection strength: {MIN_STRENGTH}{RESET}")
    LevelWatcher(symbols).run()


if __name__ == "__main__":
    main()
