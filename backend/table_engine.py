"""Per-table state machine with debouncing, timers and alerts.

The engine is pure logic: it never touches cameras or models. Each tick it receives,
per table, how many guests and how many staff were observed inside the zone
(:class:`Observation`) plus a timestamp. Both live mode (YOLO + ByteTrack) and
simulation mode feed the engine the same way, so everything downstream (dashboard,
alerts, analytics) is identical across modes.

States::

    FREE ──guests ≥ seat_confirm_sec──▶ SEATED_WAITING ──staff ≥ staff_serve_sec──▶ SERVED
      ▲                                       │                                       │
      │                          no guests ≥ leave_confirm_sec ◀──────────────────────┘
      │                                       ▼
      └──staff ≥ staff_clean_sec / manual── NEEDS_CLEANING
"""
from __future__ import annotations

import logging
import statistics
import threading
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class TableState(str, Enum):
    FREE = "FREE"
    SEATED_WAITING = "SEATED_WAITING"
    SERVED = "SERVED"
    NEEDS_CLEANING = "NEEDS_CLEANING"


OCCUPIED_STATES = (TableState.SEATED_WAITING, TableState.SERVED)


@dataclass
class Observation:
    """Raw per-tick reading for one table zone."""

    guests: int = 0
    staff: int = 0


@dataclass
class EngineSettings:
    seat_confirm_sec: float = 8
    staff_serve_sec: float = 3
    leave_confirm_sec: float = 20
    staff_clean_sec: float = 5
    debounce_window_sec: float = 3
    majority_ratio: float = 0.5
    waiter_alert_sec: float = 180
    realert_sec: float = 120
    escalate_sec: float = 300
    clean_alert_sec: float = 120

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "EngineSettings":
        """Build settings from the ``engine`` and ``alerts`` config sections."""
        merged = {**(cfg.get("engine") or {}), **(cfg.get("alerts") or {})}
        known = {k: float(v) for k, v in merged.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class Table:
    id: str
    name: str
    seats: int
    polygon: list[list[float]] = field(default_factory=list)
    state: TableState = TableState.FREE
    state_since: float = 0.0
    guest_count: int = 0
    last_staff_visit: Optional[float] = None
    session_start: Optional[float] = None
    served_at: Optional[float] = None
    # --- internal debounce bookkeeping ---
    samples: deque = field(default_factory=deque)
    guest_present: bool = False
    staff_present: bool = False
    guest_present_since: Optional[float] = None
    guest_absent_since: Optional[float] = None
    staff_present_since: Optional[float] = None
    peak_guests: int = 0

    def to_public(self, now: float) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "seats": self.seats,
            "polygon": self.polygon,
            "state": self.state.value,
            "state_since": self.state_since,
            "elapsed_sec": max(0.0, now - self.state_since),
            "guest_count": self.guest_count,
            "staff_present": self.staff_present,
            "last_staff_visit": self.last_staff_visit,
            "session_start": self.session_start,
            "session_elapsed_sec": (now - self.session_start) if self.session_start else None,
            "served_at": self.served_at,
        }


@dataclass
class Alert:
    id: int
    table_id: str
    type: str  # "waiter" | "cleaning"
    severity: str  # "warning" | "critical"
    message: str
    created_at: float
    last_notified_at: float
    acknowledged_at: Optional[float] = None
    resolved_at: Optional[float] = None
    repeat: int = 0

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "table_id": self.table_id,
            "type": self.type,
            "severity": self.severity,
            "message": self.message,
            "created_at": self.created_at,
            "last_notified_at": self.last_notified_at,
            "acknowledged_at": self.acknowledged_at,
            "resolved_at": self.resolved_at,
            "repeat": self.repeat,
        }


EventCallback = Callable[[dict[str, Any]], None]
AlertCallback = Callable[[Alert, str], None]  # kind: new | renotify | escalate | resolve | ack


class TableEngine:
    """Thread-safe state machine for all tables of one restaurant."""

    def __init__(
        self,
        tables: list[dict[str, Any]],
        settings: EngineSettings | None = None,
        *,
        db: Any = None,
        source: str = "live",
        on_event: EventCallback | None = None,
        on_alert: AlertCallback | None = None,
        start_time: float = 0.0,
    ) -> None:
        self.settings = settings or EngineSettings()
        self.db = db
        self.source = source
        self.on_event = on_event
        self.on_alert = on_alert
        self._lock = threading.RLock()
        self._alert_seq = 0
        self.tables: dict[str, Table] = {}
        self.alerts: dict[int, Alert] = {}  # active (unresolved) alerts
        self.recent_events: deque[dict[str, Any]] = deque(maxlen=50)
        self.last_update: float = start_time
        for t in tables:
            self.tables[str(t["id"])] = Table(
                id=str(t["id"]),
                name=str(t.get("name") or t["id"]),
                seats=int(t.get("seats", 4)),
                polygon=t.get("polygon", []),
                state_since=start_time,
            )

    # ------------------------------------------------------------------ API
    def update(self, now: float, observations: dict[str, Observation]) -> None:
        """Feed one tick of observations. Tables missing from the dict count as empty."""
        with self._lock:
            self.last_update = now
            for table in self.tables.values():
                obs = observations.get(table.id, Observation())
                self._debounce(table, now, obs)
                self._transition(table, now)
                self._check_alerts(table, now)

    def mark_clean(self, table_id: str, now: float) -> bool:
        """Manual "mark clean" from the dashboard. Returns False for unknown tables."""
        with self._lock:
            table = self.tables.get(table_id)
            if table is None:
                return False
            if table.state == TableState.FREE:
                return True
            duration = now - table.state_since
            self._set_state(table, TableState.FREE, now, "MANUAL_CLEAN", duration)
            table.samples.clear()
            table.guest_present = table.staff_present = False
            table.guest_present_since = table.staff_present_since = None
            table.guest_absent_since = now
            return True

    def ack_alert(self, alert_id: int, now: float) -> bool:
        """Acknowledge an active alert (it stays visible until resolved, but stops pulsing)."""
        with self._lock:
            alert = self.alerts.get(alert_id)
            if alert is None:
                return False
            alert.acknowledged_at = now
            if self.db is not None:
                self.db.update_alert(alert.id, acknowledged_at=now)
            self._emit_event(alert.table_id, "ALERT_ACK", now,
                             duration=now - alert.created_at)
            self._notify(alert, "ack")
            return True

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """Return a JSON-serialisable view of every table and active alert."""
        with self._lock:
            now = self.last_update if now is None else now
            tables = [t.to_public(now) for t in self.tables.values()]
            alerts = sorted((a.to_public() for a in self.alerts.values()),
                            key=lambda a: a["last_notified_at"], reverse=True)
            counts = {s.value: 0 for s in TableState}
            for t in self.tables.values():
                counts[t.state.value] += 1
            return {"now": now, "tables": tables, "alerts": alerts, "counts": counts,
                    "recent_events": list(self.recent_events)[-20:]}

    def get_table(self, table_id: str) -> Table | None:
        return self.tables.get(table_id)

    def set_tables(self, defs: list[dict[str, Any]], now: float) -> None:
        """Hot-reload the table list (setup page save).

        Tables whose id survives keep their live state/timers; new ones start FREE;
        removed ones disappear and their alerts are resolved.
        """
        with self._lock:
            new: dict[str, Table] = {}
            for d in defs:
                tid = str(d["id"])
                t = self.tables.get(tid) or Table(id=tid, name=tid, seats=4, state_since=now)
                t.name = str(d.get("name") or tid)
                t.seats = int(d.get("seats", t.seats))
                t.polygon = d.get("polygon", t.polygon)
                new[tid] = t
            for tid in set(self.tables) - set(new):
                for kind in ("waiter", "cleaning"):
                    self._resolve_alerts(tid, kind, now)
            self.tables = new
            log.info("Engine tables reloaded: %d tables", len(new))

    # ------------------------------------------------------------ internals
    def _debounce(self, table: Table, now: float, obs: Observation) -> None:
        """Sliding-window majority vote so single-frame misses never flip state."""
        s = self.settings
        table.samples.append((now, max(0, obs.guests), max(0, obs.staff)))
        cutoff = now - s.debounce_window_sec
        while len(table.samples) > 1 and table.samples[0][0] < cutoff:
            table.samples.popleft()
        n = len(table.samples)
        guest_hits = sum(1 for _, g, _ in table.samples if g > 0)
        staff_hits = sum(1 for _, _, st in table.samples if st > 0)
        guest_present = guest_hits / n > s.majority_ratio
        staff_present = staff_hits / n > s.majority_ratio

        # When the vote flips, backdate to the first matching sample in the window so the
        # debounce lag doesn't bias the confirm timers (a real 3 s visit counts as 3 s).
        def first(pred) -> float:
            return next((ts for ts, g, st in table.samples if pred(g, st)), now)

        if guest_present:
            counts = [g for _, g, _ in table.samples if g > 0]
            table.guest_count = int(round(statistics.median(counts)))
            table.peak_guests = max(table.peak_guests, table.guest_count)
            table.guest_absent_since = None
            if table.guest_present_since is None:
                table.guest_present_since = first(lambda g, st: g > 0)
        else:
            table.guest_count = 0
            table.guest_present_since = None
            if table.guest_absent_since is None:
                table.guest_absent_since = first(lambda g, st: g == 0)

        if staff_present:
            if table.staff_present_since is None:
                table.staff_present_since = first(lambda g, st: st > 0)
            table.last_staff_visit = now
        else:
            table.staff_present_since = None

        table.guest_present = guest_present
        table.staff_present = staff_present

    def _transition(self, table: Table, now: float) -> None:
        s = self.settings
        st = table.state
        guests_for = (now - table.guest_present_since) if table.guest_present_since is not None else 0.0
        absent_for = (now - table.guest_absent_since) if table.guest_absent_since is not None else 0.0
        staff_for = (now - table.staff_present_since) if table.staff_present_since is not None else 0.0

        if st in (TableState.FREE, TableState.NEEDS_CLEANING):
            if table.guest_present and guests_for >= s.seat_confirm_sec:
                arrived = table.guest_present_since or now
                if st == TableState.NEEDS_CLEANING:
                    # New party sat at an uncleaned table: close the cleaning phase implicitly.
                    self._emit_event(table.id, "SEATED_UNCLEANED", now,
                                     duration=arrived - table.state_since)
                table.session_start = arrived
                table.served_at = None
                table.peak_guests = table.guest_count
                self._set_state(table, TableState.SEATED_WAITING, now, "SEATED",
                                arrived - table.state_since, since=arrived)
                return
            if st == TableState.NEEDS_CLEANING and table.staff_present and staff_for >= s.staff_clean_sec:
                arrived = table.staff_present_since or now
                self._set_state(table, TableState.FREE, now, "CLEANED",
                                arrived - table.state_since)
                return

        if st == TableState.SEATED_WAITING and table.staff_present and staff_for >= s.staff_serve_sec:
            arrived = table.staff_present_since or now
            table.served_at = arrived
            self._set_state(table, TableState.SERVED, now, "SERVED",
                            arrived - table.state_since, since=arrived)
            return

        if st in OCCUPIED_STATES and not table.guest_present and absent_for >= s.leave_confirm_sec:
            left = table.guest_absent_since or now
            session = left - (table.session_start or table.state_since)
            event = "LEFT" if st == TableState.SERVED else "LEFT_UNSERVED"
            self._set_state(table, TableState.NEEDS_CLEANING, now, event, session,
                            since=left, guest_count=table.peak_guests)
            table.session_start = None

    def _set_state(self, table: Table, new: TableState, now: float, event: str,
                   duration: float, *, since: float | None = None,
                   guest_count: int | None = None) -> None:
        old = table.state
        table.state = new
        table.state_since = since if since is not None else now
        log.debug("%s: %s -> %s (%s, %.0fs)", table.id, old.value, new.value, event, duration)
        self._emit_event(table.id, event, now, duration=max(0.0, duration),
                         from_state=old.value, to_state=new.value,
                         guest_count=table.guest_count if guest_count is None else guest_count)
        # Leaving a state resolves the alerts tied to it.
        if old == TableState.SEATED_WAITING:
            self._resolve_alerts(table.id, "waiter", now)
        if old == TableState.NEEDS_CLEANING:
            self._resolve_alerts(table.id, "cleaning", now)

    # ---------------------------------------------------------------- alerts
    def _check_alerts(self, table: Table, now: float) -> None:
        s = self.settings
        elapsed = now - table.state_since
        if table.state == TableState.SEATED_WAITING and elapsed >= s.waiter_alert_sec:
            alert = self._active_alert(table.id, "waiter")
            minutes = int(elapsed // 60)
            severity = "critical" if elapsed >= s.escalate_sec else "warning"
            msg = f"Table {table.name} waiting {minutes} min with no waiter"
            if alert is None:
                alert = self._create_alert(table, "waiter", severity, msg, now)
                self._emit_event(table.id, "ALERT_WAITER", now, duration=elapsed)
                self._notify(alert, "new")
            elif severity == "critical" and alert.severity != "critical":
                alert.severity, alert.message = severity, msg
                alert.last_notified_at, alert.acknowledged_at = now, None
                self._persist_alert(alert)
                self._emit_event(table.id, "ALERT_ESCALATED", now, duration=elapsed)
                self._notify(alert, "escalate")
            elif now - alert.last_notified_at >= s.realert_sec:
                alert.repeat += 1
                alert.message, alert.last_notified_at, alert.acknowledged_at = msg, now, None
                self._persist_alert(alert)
                self._notify(alert, "renotify")
        elif table.state == TableState.NEEDS_CLEANING and elapsed >= s.clean_alert_sec:
            if self._active_alert(table.id, "cleaning") is None:
                msg = f"Table {table.name} needs cleaning"
                alert = self._create_alert(table, "cleaning", "warning", msg, now)
                self._emit_event(table.id, "ALERT_CLEANING", now, duration=elapsed)
                self._notify(alert, "new")

    def _active_alert(self, table_id: str, kind: str) -> Alert | None:
        for a in self.alerts.values():
            if a.table_id == table_id and a.type == kind:
                return a
        return None

    def _create_alert(self, table: Table, kind: str, severity: str, msg: str, now: float) -> Alert:
        payload = {"table_id": table.id, "type": kind, "severity": severity,
                   "message": msg, "created_at": now}
        if self.db is not None:
            alert_id = self.db.insert_alert(payload, source=self.source)
        else:
            self._alert_seq += 1
            alert_id = self._alert_seq
        alert = Alert(id=alert_id, table_id=table.id, type=kind, severity=severity,
                      message=msg, created_at=now, last_notified_at=now)
        self.alerts[alert_id] = alert
        log.info("ALERT #%s: %s", alert_id, msg)
        return alert

    def _persist_alert(self, alert: Alert) -> None:
        if self.db is not None:
            self.db.update_alert(alert.id, severity=alert.severity, message=alert.message,
                                 acknowledged_at=alert.acknowledged_at)

    def _resolve_alerts(self, table_id: str, kind: str, now: float) -> None:
        for alert_id in [a.id for a in self.alerts.values()
                         if a.table_id == table_id and a.type == kind]:
            alert = self.alerts.pop(alert_id)
            alert.resolved_at = now
            if self.db is not None:
                self.db.update_alert(alert.id, resolved_at=now)
            self._notify(alert, "resolve")

    # ---------------------------------------------------------------- output
    def _emit_event(self, table_id: str, event_type: str, now: float, *,
                    duration: float | None = None, from_state: str | None = None,
                    to_state: str | None = None, guest_count: int | None = None) -> None:
        table = self.tables.get(table_id)
        event = {"table_id": table_id, "seats": table.seats if table else None,
                 "event_type": event_type, "from_state": from_state, "to_state": to_state,
                 "timestamp": now, "duration_sec": duration, "guest_count": guest_count,
                 "source": self.source}
        self.recent_events.append(event)
        if self.db is not None:
            try:
                self.db.insert_event(table_id, event_type, now, seats=event["seats"],
                                     from_state=from_state, to_state=to_state,
                                     duration_sec=duration, guest_count=guest_count,
                                     source=self.source)
            except Exception:  # never let a DB hiccup stop the engine
                log.exception("Failed to write event %s", event_type)
        if self.on_event:
            self.on_event(event)

    def _notify(self, alert: Alert, kind: str) -> None:
        if self.on_alert:
            try:
                self.on_alert(alert, kind)
            except Exception:
                log.exception("Alert callback failed")
