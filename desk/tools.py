"""
Tools the desk agent can call. Arguments are plain strings/numbers so a
local model can call them reliably.
"""
import json
from datetime import datetime, timezone
from typing import Dict, Any

from . import store


def _price_info(symbol: str) -> Dict[str, Any]:
    p = store.get_price(symbol)
    if not p:
        return {"last_price": None, "price_age_seconds": None}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["updated_at"])).total_seconds()
    return {"last_price": p["last_price"], "price_age_seconds": int(age)}


def add_level(symbol: str, price: float, label: str = "", kind: str = "other", note: str = "") -> Dict[str, Any]:
    """
    Save a price level for the watcher to monitor.

    Args:
        symbol: Instrument root, e.g. "NQ" or "MGC".
        price: The level price, e.g. 21450.25.
        label: Short name for the level, e.g. "weekly high" or "VAH".
        kind: Category: "weekly", "volume", "daily", or "other".
        note: Optional context from the trader about why the level matters.

    Returns:
        The saved level, including its id.
    """
    level = store.add_level(symbol, price, label, kind, note)
    return {"saved": level}


def list_levels(symbol: str = "") -> Dict[str, Any]:
    """
    List the active levels being watched, highest price first, with each
    level's distance from the last price.

    Args:
        symbol: Instrument root, e.g. "NQ". Empty string lists all symbols.

    Returns:
        Active levels and the last known price per symbol.
    """
    levels = store.list_levels(symbol or None)
    prices: Dict[str, Any] = {}
    for lv in levels:
        sym = lv["symbol"]
        if sym not in prices:
            prices[sym] = _price_info(sym)
        last = prices[sym]["last_price"]
        lv["distance_from_last"] = round(lv["price"] - last, 4) if last is not None else None
    return {"levels": levels, "prices": prices}


def remove_level(level_id: int) -> Dict[str, Any]:
    """
    Stop watching one level.

    Args:
        level_id: The id of the level (from list_levels).
    """
    return {"removed": store.deactivate_level(level_id), "level_id": level_id}


def clear_levels(symbol: str, kind: str = "") -> Dict[str, Any]:
    """
    Stop watching all levels for a symbol, e.g. before loading a new day's levels.

    Args:
        symbol: Instrument root, e.g. "NQ".
        kind: Only clear this category ("weekly", "volume", ...). Empty clears all.
    """
    count = store.deactivate_levels(symbol, kind or None)
    return {"cleared": count, "symbol": symbol.upper(), "kind": kind or "all"}


def get_market_snapshot(symbol: str) -> Dict[str, Any]:
    """
    Get the last price streamed by the watcher, the nearest watched level
    above and below it, and today's level rejections/acceptances.

    Args:
        symbol: Instrument root, e.g. "NQ".

    Returns:
        "summary": a plain-English answer to relay to the trader as-is, plus
        the underlying data (last price, nearest levels, today's level events).
    """
    sym = symbol.upper()
    info = _price_info(sym)
    last = info["last_price"]
    levels = store.list_levels(sym)
    active_ids = {lv["id"] for lv in levels}
    above = [lv for lv in levels if last is not None and lv["price"] > last]
    below = [lv for lv in levels if last is not None and lv["price"] <= last]
    nearest_above = min(above, key=lambda lv: lv["price"]) if above else None
    nearest_below = max(below, key=lambda lv: lv["price"]) if below else None
    running = info["price_age_seconds"] is not None and info["price_age_seconds"] < 120
    today = datetime.now(timezone.utc).date().isoformat()
    # Only events on levels that are still being watched matter
    events = [
        a for a in reversed(store.recent_alerts(50, sym))
        if a["created_at"].startswith(today) and a["level_id"] in active_ids
    ]

    def px(x):
        return f"{x:.4f}".rstrip("0").rstrip(".")

    def fmt(lv):
        return f"{lv['label'] or 'level'} {px(lv['price'])}"

    lines = []
    if last is None:
        lines.append(f"No price for {sym} yet. Start the watcher: python -m desk.watcher {sym}")
    else:
        lines.append(f"{sym} last price: {px(last)}.")
        if not running:
            lines.append(f"WARNING: the watcher is not running, so this price is "
                         f"{info['price_age_seconds'] // 60} minutes old. Start it with: python -m desk.watcher {sym}")
        lines.append(f"Nearest level above: {fmt(nearest_above)}." if nearest_above else "No watched levels above.")
        lines.append(f"Nearest level below: {fmt(nearest_below)}." if nearest_below else "No watched levels below.")
    if events:
        lines.append("Level events today: " + "; ".join(describe_alert(a) for a in events) + ".")
    else:
        lines.append("No watched level has been rejected or accepted today.")

    return {
        "summary": " ".join(lines),
        "symbol": sym,
        **info,
        "watcher_running": running,
        "nearest_above": nearest_above,
        "nearest_below": nearest_below,
        "level_events_today": events,
    }


def describe_alert(a: Dict[str, Any]) -> str:
    """One-line description of a rejection/acceptance alert, in local time."""
    candle = json.loads(a["candle"]) if a.get("candle") else {}
    if candle.get("start"):
        when = "the " + datetime.fromtimestamp(candle["start"]).strftime("%H:%M") + f" {a['timeframe']} candle"
    else:
        when = "the candle recorded at " + datetime.fromisoformat(a["created_at"]).astimezone().strftime("%H:%M")
    price = lambda x: f"{x:.4f}".rstrip("0").rstrip(".")
    outcome = (a["outcome"] or "broken").upper()
    if a.get("strength"):
        outcome += f" [{a['strength']}]"
    return (f"{a['label'] or 'level'} {price(a['level_price'])} {outcome} "
            f"({a['direction']}, {a['pattern'] or 'no pattern'}) on {when}, closing {price(a['price'])}")


def get_recent_alerts(limit: int = 10, symbol: str = "") -> Dict[str, Any]:
    """
    Get the most recent level rejections/acceptances from the watcher, newest first.

    Args:
        limit: How many alerts to return.
        symbol: Instrument root to filter by. Empty string returns all.

    Returns:
        "summary" lines to relay to the trader, plus the raw alerts.
    """
    alerts = store.recent_alerts(limit, symbol or None)
    summary = [f"{a['symbol']} {describe_alert(a)}" for a in alerts] or ["No level events recorded."]
    return {"summary": summary, "alerts": alerts}


def add_note(text: str, symbol: str = "") -> Dict[str, Any]:
    """
    Save a journal note: a thesis, an observation, or a trade review.

    Args:
        text: The note, in the trader's words.
        symbol: Instrument root it relates to, if any.
    """
    return {"saved": store.add_note(text, symbol)}


def get_recent_notes(limit: int = 10, symbol: str = "") -> Dict[str, Any]:
    """
    Get recent journal notes, newest first.

    Args:
        limit: How many notes to return.
        symbol: Instrument root to filter by. Empty string returns all.
    """
    return {"notes": store.recent_notes(limit, symbol or None)}


DESK_TOOLS = [
    add_level,
    list_levels,
    remove_level,
    clear_levels,
    get_market_snapshot,
    get_recent_alerts,
    add_note,
    get_recent_notes,
]
