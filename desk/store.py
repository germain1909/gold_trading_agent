"""
SQLite storage shared by the chat agent and the market watcher.

The chat agent writes levels; the watcher reads them, writes the latest
price, and records an alert whenever price breaks a level.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DESK_DB_PATH", os.path.join(PROJECT_ROOT, "data", "desk.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS levels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'other',
    note TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    level_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    level_price REAL NOT NULL,
    price REAL NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',
    strength TEXT NOT NULL DEFAULT '',
    pattern TEXT NOT NULL DEFAULT '',
    timeframe TEXT NOT NULL DEFAULT '',
    candle TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    last_price REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)")}
    for col in ("outcome", "strength", "pattern", "timeframe", "candle"):
        if col not in cols:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


def _rows(rows) -> List[Dict[str, Any]]:
    return [dict(r) for r in rows]


# ---------- levels ----------

def add_level(symbol: str, price: float, label: str = "", kind: str = "other", note: str = "") -> Dict[str, Any]:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO levels (symbol, price, label, kind, note, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (symbol.upper(), float(price), label, kind.lower(), note, now_iso()),
        )
        row = conn.execute("SELECT * FROM levels WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_levels(symbol: Optional[str] = None, active_only: bool = True) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM levels WHERE 1=1"
    args: list = []
    if symbol:
        sql += " AND symbol = ?"
        args.append(symbol.upper())
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY symbol, price DESC"
    with connect() as conn:
        return _rows(conn.execute(sql, args).fetchall())


def deactivate_level(level_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE levels SET active = 0 WHERE id = ? AND active = 1", (int(level_id),))
    return cur.rowcount > 0


def deactivate_levels(symbol: str, kind: Optional[str] = None) -> int:
    sql = "UPDATE levels SET active = 0 WHERE symbol = ? AND active = 1"
    args: list = [symbol.upper()]
    if kind:
        sql += " AND kind = ?"
        args.append(kind.lower())
    with connect() as conn:
        cur = conn.execute(sql, args)
    return cur.rowcount


# ---------- alerts ----------

def add_alert(
    level: Dict[str, Any],
    outcome: str,
    bias: str,
    price: float,
    pattern: str = "",
    timeframe: str = "",
    strength: str = "",
    candle: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Record what a closed candle did at a level.
    outcome: "rejected" | "accepted"; bias (stored as direction): "bullish" | "bearish".
    """
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (level_id, symbol, direction, level_price, price, label, "
            "outcome, strength, pattern, timeframe, candle, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (level["id"], level["symbol"], bias, level["price"], price, level["label"],
             outcome, strength, pattern, timeframe, json.dumps(candle or {}), now_iso()),
        )
        row = conn.execute("SELECT * FROM alerts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def recent_alerts(limit: int = 10, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM alerts"
    args: list = []
    if symbol:
        sql += " WHERE symbol = ?"
        args.append(symbol.upper())
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with connect() as conn:
        return _rows(conn.execute(sql, args).fetchall())


# ---------- prices ----------

def set_price(symbol: str, contract_id: str, last_price: float) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO prices (symbol, contract_id, last_price, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(symbol) DO UPDATE SET contract_id = excluded.contract_id, "
            "last_price = excluded.last_price, updated_at = excluded.updated_at",
            (symbol.upper(), contract_id, float(last_price), now_iso()),
        )


def get_price(symbol: str) -> Optional[Dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM prices WHERE symbol = ?", (symbol.upper(),)).fetchone()
    return dict(row) if row else None


# ---------- notes ----------

def add_note(text: str, symbol: str = "") -> Dict[str, Any]:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO notes (symbol, text, created_at) VALUES (?, ?, ?)",
            (symbol.upper(), text, now_iso()),
        )
        row = conn.execute("SELECT * FROM notes WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def recent_notes(limit: int = 10, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM notes"
    args: list = []
    if symbol:
        sql += " WHERE symbol = ?"
        args.append(symbol.upper())
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with connect() as conn:
        return _rows(conn.execute(sql, args).fetchall())
