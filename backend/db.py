"""SQLite persistence: table events and alerts only (never video or faces)."""
from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id     TEXT    NOT NULL,
    seats        INTEGER,
    event_type   TEXT    NOT NULL,
    from_state   TEXT,
    to_state     TEXT,
    timestamp    REAL    NOT NULL,
    duration_sec REAL,
    guest_count  INTEGER,
    source       TEXT    DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, timestamp);

CREATE TABLE IF NOT EXISTS alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id         TEXT NOT NULL,
    type             TEXT NOT NULL,
    severity         TEXT NOT NULL,
    message          TEXT NOT NULL,
    created_at       REAL NOT NULL,
    acknowledged_at  REAL,
    resolved_at      REAL,
    source           TEXT DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class Database:
    """Thread-safe wrapper around a single SQLite connection."""

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        log.info("SQLite ready at %s", self.path)

    # ---- generic helpers -------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        """Run a write statement and return lastrowid."""
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur.lastrowid or 0

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    # ---- domain helpers --------------------------------------------------
    def insert_event(
        self,
        table_id: str,
        event_type: str,
        timestamp: float,
        *,
        seats: int | None = None,
        from_state: str | None = None,
        to_state: str | None = None,
        duration_sec: float | None = None,
        guest_count: int | None = None,
        source: str = "live",
    ) -> int:
        return self.execute(
            "INSERT INTO events (table_id, seats, event_type, from_state, to_state, timestamp,"
            " duration_sec, guest_count, source) VALUES (?,?,?,?,?,?,?,?,?)",
            (table_id, seats, event_type, from_state, to_state, timestamp,
             duration_sec, guest_count, source),
        )

    def insert_alert(self, alert: dict[str, Any], source: str = "live") -> int:
        return self.execute(
            "INSERT INTO alerts (table_id, type, severity, message, created_at, source)"
            " VALUES (?,?,?,?,?,?)",
            (alert["table_id"], alert["type"], alert["severity"], alert["message"],
             alert["created_at"], source),
        )

    def update_alert(self, alert_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE alerts SET {cols} WHERE id=?", (*fields.values(), alert_id))

    def get_meta(self, key: str) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
