"""Walk-in queue management and "next table free" predictions.

* :class:`QueueManager` — thread-safe waiting list (host-added parties + simulated walk-ins).
* :class:`HistoryStats` — historical averages from SQLite (session length per table size
  and hour, cleaning delay), cached and refreshed periodically.
* :func:`predict` — pure function: ranks tables by estimated time-to-free and assigns each
  queued party a suggested table (smallest that fits) plus an estimated wait.

Time-to-free for an occupied table::

    avg_session(seats, hour) - elapsed_session + avg_cleaning
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Protocol

log = logging.getLogger(__name__)

OCCUPIED = ("SEATED_WAITING", "SERVED")
MIN_OCCUPIED_ETA_SEC = 120.0  # an overdue table is still "a couple of minutes" away
MIN_CLEANING_ETA_SEC = 30.0
NEAR_TIE_SEC = 5 * 60  # prefer a smaller table if it frees within 5 min of the earliest


# --------------------------------------------------------------------- queue
@dataclass
class QueueEntry:
    id: int
    name: str
    size: int
    added_at: float
    source: str = "host"  # host | sim
    patience_sec: Optional[float] = None  # None = waits indefinitely
    note: str = ""


class QueueManager:
    """FIFO waiting list shared by the dashboard (host) and the simulator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._entries: list[QueueEntry] = []
        self.history: deque[dict[str, Any]] = deque(maxlen=50)

    def add(self, name: str, size: int, now: float, *, source: str = "host",
            patience_sec: float | None = None, note: str = "") -> QueueEntry:
        name = (name or "").strip()[:40] or "Guest"
        size = max(1, min(20, int(size)))
        with self._lock:
            entry = QueueEntry(next(self._ids), name, size, now, source, patience_sec, note[:80])
            self._entries.append(entry)
        log.info("Queue + %s (%d) [%s]", entry.name, entry.size, source)
        return entry

    def remove(self, entry_id: int, now: float, *, reason: str = "removed",
               table_id: str | None = None) -> QueueEntry | None:
        with self._lock:
            for i, e in enumerate(self._entries):
                if e.id == entry_id:
                    del self._entries[i]
                    self.history.append({"id": e.id, "name": e.name, "size": e.size,
                                         "reason": reason, "table_id": table_id,
                                         "waited_sec": now - e.added_at, "at": now})
                    return e
        return None

    def expire(self, now: float) -> int:
        """Drop parties whose patience ran out (they walked away). Returns count."""
        with self._lock:
            gone = [e for e in self._entries
                    if e.patience_sec is not None and now - e.added_at >= e.patience_sec]
        for e in gone:
            self.remove(e.id, now, reason="walked_away")
        return len(gone)

    def entries(self) -> list[QueueEntry]:
        with self._lock:
            return list(self._entries)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ------------------------------------------------------------------- history
class StatsSource(Protocol):
    def avg_session_sec(self, seats: int, hour: int) -> float: ...
    def avg_cleaning_sec(self) -> float: ...


class HistoryStats:
    """Historical averages read from the ``events`` table, cached for ``ttl`` seconds."""

    def __init__(self, db: Any, *, fallback_session_min: float = 50,
                 fallback_cleaning_min: float = 3, min_samples: int = 3, ttl: float = 60) -> None:
        self.db = db
        self.fallback_session = fallback_session_min * 60
        self.fallback_cleaning = fallback_cleaning_min * 60
        self.min_samples = min_samples
        self.ttl = ttl
        self._loaded_at = -1e9
        self._by_seats_hour: dict[tuple[int, int], float] = {}
        self._by_seats: dict[int, float] = {}
        self._cleaning: float | None = None
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        if time.monotonic() - self._loaded_at < self.ttl or self.db is None:
            return
        try:
            rows = self.db.query(
                "SELECT seats, CAST(strftime('%H', timestamp - duration_sec, 'unixepoch', 'localtime')"
                " AS INTEGER) AS h, AVG(duration_sec) AS avg, COUNT(*) AS n FROM events"
                " WHERE event_type='LEFT' AND duration_sec > 300 AND seats IS NOT NULL"
                " GROUP BY seats, h")
            by_sh: dict[tuple[int, int], float] = {}
            agg: dict[int, list[float]] = {}
            for r in rows:
                agg.setdefault(r["seats"], [0.0, 0.0])
                agg[r["seats"]][0] += r["avg"] * r["n"]
                agg[r["seats"]][1] += r["n"]
                if r["n"] >= self.min_samples:
                    by_sh[(r["seats"], r["h"])] = r["avg"]
            by_s = {s: tot / n for s, (tot, n) in agg.items() if n >= self.min_samples}
            clean = self.db.query(
                "SELECT AVG(duration_sec) AS avg, COUNT(*) AS n FROM events"
                " WHERE event_type IN ('CLEANED', 'MANUAL_CLEAN') AND duration_sec IS NOT NULL")
            cleaning = clean[0]["avg"] if clean and clean[0]["n"] >= self.min_samples else None
            with self._lock:
                self._by_seats_hour, self._by_seats, self._cleaning = by_sh, by_s, cleaning
        except Exception:
            log.exception("Could not load history stats; using fallbacks")
        self._loaded_at = time.monotonic()

    def avg_session_sec(self, seats: int, hour: int) -> float:
        self._refresh()
        with self._lock:
            return self._by_seats_hour.get((seats, hour)) or self._by_seats.get(seats) or self.fallback_session

    def avg_cleaning_sec(self) -> float:
        self._refresh()
        with self._lock:
            # + ~1 min for the actual wipe-down after staff arrives
            return (self._cleaning + 60) if self._cleaning is not None else self.fallback_cleaning


class FixedStats:
    """Constant stats — handy for tests and for running without a database."""

    def __init__(self, session_sec: float = 3000, cleaning_sec: float = 180) -> None:
        self.session_sec, self.cleaning_sec = session_sec, cleaning_sec

    def avg_session_sec(self, seats: int, hour: int) -> float:
        return self.session_sec

    def avg_cleaning_sec(self) -> float:
        return self.cleaning_sec


# ---------------------------------------------------------------- prediction
def time_to_free(table: dict[str, Any], now: float, stats: StatsSource) -> float:
    """Estimated seconds until ``table`` (engine public dict) is FREE again."""
    state = table["state"]
    if state == "FREE":
        return 0.0
    cleaning = stats.avg_cleaning_sec()
    if state == "NEEDS_CLEANING":
        return max(MIN_CLEANING_ETA_SEC, cleaning - table.get("elapsed_sec", 0.0))
    start = table.get("session_start") or table.get("state_since") or now
    hour = datetime.fromtimestamp(start).hour
    remaining = stats.avg_session_sec(int(table["seats"]), hour) - (now - start)
    return max(MIN_OCCUPIED_ETA_SEC, remaining) + cleaning


def predict(tables: Iterable[dict[str, Any]], queue: Iterable[QueueEntry], now: float,
            stats: StatsSource) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(predictions, queue_view)``.

    ``predictions``: non-free tables ranked by estimated time-to-free.
    ``queue_view``: each queued party with ``suggested_table`` and ``est_wait_sec``.
    Parties are assigned greedily in FIFO order; once a table is promised to a party it
    becomes available again only after that party's predicted meal + cleaning.
    """
    tables = list(tables)
    cleaning = stats.avg_cleaning_sec()
    hour = datetime.fromtimestamp(now).hour
    etas = {t["id"]: time_to_free(t, now, stats) for t in tables}

    predictions = sorted(
        ({"table_id": t["id"], "name": t["name"], "seats": t["seats"], "state": t["state"],
          "eta_sec": round(etas[t["id"]]), "eta_at": now + etas[t["id"]]}
         for t in tables if t["state"] != "FREE"),
        key=lambda p: (p["eta_sec"], p["seats"]))

    avail = dict(etas)  # table_id -> seconds until it can take the next party
    by_id = {t["id"]: t for t in tables}
    queue_view: list[dict[str, Any]] = []
    for e in queue:
        fits = [tid for tid, t in by_id.items() if int(t["seats"]) >= e.size]
        row = asdict(e)
        row["waited_sec"] = now - e.added_at
        if not fits:
            reason = "No tables set up yet" if not by_id else "No table large enough — combine tables"
            row.update(suggested_table=None, suggested_name=None, est_wait_sec=None,
                       note=row["note"] or reason)
            queue_view.append(row)
            continue
        earliest = min(avail[tid] for tid in fits)
        near = [tid for tid in fits if avail[tid] <= earliest + NEAR_TIE_SEC]
        best = min(near, key=lambda tid: (int(by_id[tid]["seats"]), avail[tid]))
        wait = avail[best]
        row.update(suggested_table=best, suggested_name=by_id[best]["name"],
                   est_wait_sec=round(wait))
        queue_view.append(row)
        avail[best] = wait + stats.avg_session_sec(int(by_id[best]["seats"]), hour) + cleaning
    return predictions, queue_view
