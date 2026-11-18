"""Table state machine: transitions, debouncing, alerts — driven by fake detections."""
from __future__ import annotations

import pytest

from backend.db import Database
from backend.table_engine import EngineSettings, Observation, TableEngine, TableState

T0 = 1_000_000.0


class Harness:
    """Feeds one observation per second to a single-table engine and records callbacks."""

    def __init__(self, **overrides: float) -> None:
        self.events: list[dict] = []
        self.alerts: list[tuple[str, str, str]] = []  # (type, kind, severity)
        self.now = T0
        self.engine = TableEngine(
            [{"id": "T1", "name": "T1", "seats": 4}, {"id": "T2", "name": "T2", "seats": 2}],
            EngineSettings(**overrides), start_time=T0,
            on_event=self.events.append,
            on_alert=lambda a, k: self.alerts.append((a.type, k, a.severity)),
        )

    def run(self, seconds: int, guests: int = 0, staff: int = 0, table: str = "T1") -> None:
        for _ in range(seconds):
            self.now += 1
            self.engine.update(self.now, {table: Observation(guests, staff)})

    @property
    def t1(self):
        return self.engine.tables["T1"]

    def types(self) -> list[str]:
        return [e["event_type"] for e in self.events]


# ------------------------------------------------------------------ seating
def test_guests_must_stay_seat_confirm_seconds_before_seated() -> None:
    h = Harness()
    h.run(8, guests=2)  # debounced presence starts at t+1, so 8 ticks = 7 s confirmed
    assert h.t1.state == TableState.FREE
    h.run(2, guests=2)
    assert h.t1.state == TableState.SEATED_WAITING
    assert h.types() == ["SEATED"]
    assert h.t1.session_start == pytest.approx(T0 + 1)
    assert h.t1.guest_count == 2


def test_single_frame_ghost_does_not_seat_a_free_table() -> None:
    h = Harness()
    for _ in range(10):
        h.run(1, guests=1)
        h.run(3, guests=0)
    assert h.t1.state == TableState.FREE
    assert h.events == []


def test_missed_detections_do_not_flicker_an_occupied_table() -> None:
    h = Harness()
    h.run(12, guests=3)
    assert h.t1.state == TableState.SEATED_WAITING
    for _ in range(30):  # detector misses everyone in 1 of every 4 frames
        h.run(3, guests=3)
        h.run(1, guests=0)
    assert h.t1.state == TableState.SEATED_WAITING
    assert h.types() == ["SEATED"]


# ------------------------------------------------------------------ serving
def test_staff_visit_of_three_seconds_marks_table_served() -> None:
    h = Harness()
    h.run(12, guests=2)
    h.run(3, guests=2, staff=1)
    assert h.t1.state == TableState.SEATED_WAITING  # 2 s of confirmed presence so far
    h.run(2, guests=2, staff=1)
    assert h.t1.state == TableState.SERVED
    served = h.events[-1]
    assert served["event_type"] == "SERVED"
    assert served["duration_sec"] == pytest.approx(12.0)  # response time: seat→staff arrival
    assert h.t1.last_staff_visit == pytest.approx(h.now)


def test_staff_walking_past_briefly_does_not_count_as_served() -> None:
    h = Harness()
    h.run(12, guests=2)
    h.run(2, guests=2, staff=1)
    h.run(5, guests=2)
    assert h.t1.state == TableState.SEATED_WAITING


# ------------------------------------------------------------------ leaving
def test_guests_leaving_for_leave_confirm_marks_needs_cleaning() -> None:
    h = Harness()
    h.run(12, guests=2)
    h.run(5, guests=2, staff=1)
    h.run(600, guests=2)
    h.run(20, guests=0)
    assert h.t1.state == TableState.SERVED  # still within leave_confirm_sec
    h.run(3, guests=0)
    assert h.t1.state == TableState.NEEDS_CLEANING
    left = h.events[-1]
    assert left["event_type"] == "LEFT"
    assert left["duration_sec"] == pytest.approx(617, abs=2)  # session length
    assert left["guest_count"] == 2
    assert h.t1.session_start is None


def test_party_leaving_before_service_is_logged_as_unserved() -> None:
    h = Harness()
    h.run(12, guests=2)
    h.run(25, guests=0)
    assert h.t1.state == TableState.NEEDS_CLEANING
    assert h.types()[-1] == "LEFT_UNSERVED"


# ----------------------------------------------------------------- cleaning
def test_staff_cleaning_for_five_seconds_frees_the_table() -> None:
    h = Harness()
    h.run(12, guests=2)
    h.run(25, guests=0)
    h.run(60)
    h.run(4, staff=1)
    assert h.t1.state == TableState.NEEDS_CLEANING
    h.run(3, staff=1)
    assert h.t1.state == TableState.FREE
    assert h.events[-1]["event_type"] == "CLEANED"


def test_manual_mark_clean() -> None:
    h = Harness()
    h.run(12, guests=2)
    h.run(25, guests=0)
    assert h.engine.mark_clean("T1", h.now)
    assert h.t1.state == TableState.FREE
    assert h.events[-1]["event_type"] == "MANUAL_CLEAN"
    assert not h.engine.mark_clean("NOPE", h.now)


def test_new_party_at_uncleaned_table_goes_straight_to_seated() -> None:
    h = Harness()
    h.run(12, guests=2)
    h.run(25, guests=0)
    h.run(12, guests=4)
    assert h.t1.state == TableState.SEATED_WAITING
    assert h.types()[-2:] == ["SEATED_UNCLEANED", "SEATED"]


# ------------------------------------------------------------------- alerts
def test_waiter_alert_renotify_escalate_and_resolve() -> None:
    h = Harness(waiter_alert_sec=180, realert_sec=120, escalate_sec=300)
    h.run(10, guests=2)                      # seated; state_since is backdated to arrival (T0+1)
    h.run(172, guests=2)                     # > 180 s since arrival
    assert ("waiter", "new", "warning") in h.alerts
    alert = next(iter(h.engine.alerts.values()))
    assert "waiting 3 min" in alert.message
    assert h.engine.ack_alert(alert.id, h.now)
    assert alert.acknowledged_at is not None
    h.run(125, guests=2)                     # past 300 s → escalated (re-opens the ack)
    assert ("waiter", "escalate", "critical") in h.alerts
    assert alert.acknowledged_at is None
    h.run(125, guests=2)                     # another reminder interval
    assert ("waiter", "renotify", "critical") in h.alerts
    assert alert.repeat >= 1
    h.run(5, guests=2, staff=1)
    assert h.t1.state == TableState.SERVED
    assert ("waiter", "resolve", "critical") in h.alerts
    assert h.engine.alerts == {}
    assert "ALERT_WAITER" in h.types() and "ALERT_ESCALATED" in h.types()


def test_cleaning_alert_raised_and_resolved() -> None:
    h = Harness(clean_alert_sec=120)
    h.run(12, guests=2)
    h.run(25, guests=0)                      # left at T0+13 (state_since backdated)
    h.run(90)                                # T0+127: 114 s dirty
    assert not h.engine.alerts
    h.run(10)                                # T0+137: > 120 s dirty
    assert [a.type for a in h.engine.alerts.values()] == ["cleaning"]
    h.run(7, staff=1)
    assert h.t1.state == TableState.FREE
    assert not h.engine.alerts


def test_ack_unknown_alert_returns_false() -> None:
    assert not Harness().engine.ack_alert(999, T0)


# ----------------------------------------------------------- misc & storage
def test_tables_are_independent() -> None:
    h = Harness()
    h.run(12, guests=2, table="T2")
    assert h.engine.tables["T2"].state == TableState.SEATED_WAITING
    assert h.t1.state == TableState.FREE


def test_hot_reload_keeps_state_of_surviving_tables() -> None:
    h = Harness()
    h.run(12, guests=2)
    h.engine.set_tables([{"id": "T1", "name": "Window", "seats": 6}, {"id": "T9", "seats": 2}], h.now)
    assert set(h.engine.tables) == {"T1", "T9"}
    assert h.t1.state == TableState.SEATED_WAITING and h.t1.name == "Window" and h.t1.seats == 6
    assert h.engine.tables["T9"].state == TableState.FREE


def test_events_and_alerts_persist_to_sqlite() -> None:
    db = Database(":memory:")
    eng = TableEngine([{"id": "T1", "seats": 4}], EngineSettings(waiter_alert_sec=30), db=db,
                      source="test", start_time=T0)
    now = T0
    for _ in range(60):
        now += 1
        eng.update(now, {"T1": Observation(2, 0)})
    rows = db.query("SELECT event_type, source, seats FROM events ORDER BY id")
    assert [r["event_type"] for r in rows] == ["SEATED", "ALERT_WAITER"]
    assert rows[0]["source"] == "test" and rows[0]["seats"] == 4
    assert db.query("SELECT type FROM alerts")[0]["type"] == "waiter"


def test_snapshot_counts() -> None:
    h = Harness()
    h.run(12, guests=2)
    snap = h.engine.snapshot(h.now)
    assert snap["counts"] == {"FREE": 1, "SEATED_WAITING": 1, "SERVED": 0, "NEEDS_CLEANING": 0}
    assert {t["id"] for t in snap["tables"]} == {"T1", "T2"}
