"""Zone assignment, staff classifier, simulator, analytics and an API smoke test."""
from __future__ import annotations

import numpy as np
import pytest

from backend.analytics import seed_demo_history, weekly_report
from backend.config import Config
from backend.db import Database
from backend.detector import Person
from backend.live_pipeline import assign_zones, build_observations, zones_to_pixels
from backend.simulator import Simulator, build_layout
from backend.staff_classifier import UniformColorClassifier, build_classifier
from backend.table_engine import EngineSettings, TableEngine

ZONES = [{"id": "T1", "name": "T1", "seats": 4, "polygon": [[0.1, 0.5], [0.4, 0.5], [0.4, 0.9], [0.1, 0.9]]}]


def test_zone_assignment_uses_foot_point_and_adjacency() -> None:
    zones = zones_to_pixels(ZONES, 1000, 500)
    inside = Person(1, (150, 150, 250, 300), 0.9)    # foot (200, 300) inside
    adjacent = Person(2, (410, 50, 440, 260), 0.9)   # foot 25 px right of the zone
    far = Person(3, (800, 50, 840, 200), 0.9)
    assign_zones([inside, adjacent, far], zones, adjacent_px=40)
    assert (inside.zone_id, adjacent.zone_id, far.zone_id) == ("T1", None, None)
    assert adjacent.near_zone_id == "T1" and far.near_zone_id is None
    adjacent.is_staff = True
    obs = build_observations([inside, adjacent, far], zones)
    assert (obs["T1"].guests, obs["T1"].staff) == (1, 1)


def test_uniform_colour_classifier_and_hue_wraparound() -> None:
    frame = np.full((200, 200, 3), 200, np.uint8)
    frame[20:80, 20:80] = (30, 20, 220)  # red shirt (BGR) — hue ~179, wraps around 0
    p = Person(1, (20, 20, 80, 140), 0.9)
    clf = UniformColorClassifier([170, 100, 80], [8, 255, 255])  # lower hue > upper → wrap
    assert clf.ratio(frame, p) > 0.9
    assert clf.score(frame, p, 0) > 0.9
    plain = Person(2, (100, 20, 160, 140), 0.9)
    assert clf.ratio(frame, plain) == 0


def test_posture_classifier_confirms_seated_guest() -> None:
    clf = build_classifier({"staff": {"method": "posture", "seated_guest_sec": 5}})
    frame = np.zeros((100, 100, 3), np.uint8)
    for t in range(10):
        p = Person(7, (10, 10, 60, 60), 0.9, zone_id="T1")  # square box = seated, still
        clf.classify_all(frame, [p], float(t))
    assert not p.is_staff and p.seated


def test_fidgeting_diner_stays_guest_and_walker_is_staff() -> None:
    """Regression (real CCTV clip): eating/gesturing + box jitter must not look like walking."""
    import random
    rnd = random.Random(1)
    clf = build_classifier({"staff": {"method": "posture"}})
    frame = np.zeros((540, 960, 3), np.uint8)
    diner = walker = None
    for i in range(45):  # 15 s at 3 fps
        t = i / 3
        j = rnd.uniform(-18, 18)  # ±18 px jitter on a 200 px tall, tall-looking diner
        diner = Person(1, (500 + j, 200 + j, 590 + j, 400), 0.9, zone_id="T2")
        walker = Person(2, (100 + i * 12, 100, 150 + i * 12, 300), 0.9)  # crosses the room
        clf.classify_all(frame, [diner, walker], t)
    assert not diner.is_staff
    assert walker.is_staff


def test_box_cut_off_by_frame_edge_is_not_read_as_standing() -> None:
    clf = build_classifier({"staff": {"method": "posture"}})
    frame = np.zeros((540, 960, 3), np.uint8)
    for i in range(30):
        p = Person(3, (450, 300, 520, 540), 0.9, zone_id="T3")  # tall box touching the bottom
        clf.classify_all(frame, [p], i / 3)
    assert p.seated and not p.is_staff


def test_simulator_feeds_engine_through_full_cycle() -> None:
    cfg = Config({"simulation": {"seed": 3, "start_time": "19:00", "speed": 1}})
    sim = Simulator(cfg)
    events: list[str] = []
    eng = TableEngine(sim.layout(), EngineSettings(), start_time=sim.now(),
                      on_event=lambda e: events.append(e["event_type"]))
    sim.attach(eng)
    sim.advance(2 * 3600)
    for kind in ("SEATED", "SERVED", "LEFT", "CLEANED"):
        assert kind in events
    assert "LEFT_UNSERVED" not in events  # noise must never fake a walk-out
    assert len(sim.layout()) == 16


@pytest.fixture(scope="module")
def seeded_db() -> Database:
    db = Database(":memory:")
    tables = [{"id": t.id, "name": t.name, "seats": t.seats} for t in build_layout()]
    cfg = Config({"engine": {}, "alerts": {}, "analytics": {"tables_per_waiter": 4, "avg_bill": 600}})
    assert seed_demo_history(db, tables, cfg, weeks=4) > 1000
    assert seed_demo_history(db, tables, cfg, weeks=4) == 0  # idempotent
    db.tables_def = tables  # type: ignore[attr-defined]
    db.cfg = cfg  # type: ignore[attr-defined]
    return db


def test_weekly_report_is_consistent(seeded_db: Database) -> None:
    rep = weekly_report(seeded_db, seeded_db.tables_def, seeded_db.cfg, weeks=4)  # type: ignore[attr-defined]
    k = rep["kpis"]
    assert k["covers"] > 0 and 30 < k["avg_turn_min"] < 120 and 0 < k["utilisation_pct"] <= 100
    assert len(rep["daily"]["dates"]) == 28
    for s in rep["staffing"]["shifts"]:
        assert s["weekday_peak_tables"] <= 16 and s["weekend_peak_tables"] <= 16
        assert 0 <= s["weekday_waiters"] <= 4
    rev = rep["revenue"]
    assert rev["available"] and rev["after"]["response_min"] < rev["before"]["response_min"]
    assert rev["extra_revenue_per_week"] >= 0


def test_api_smoke() -> None:
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as client:
        state = client.get("/api/state").json()
        assert state["mode"] == "simulation" and len(state["tables"]) == 16
        r = client.post("/api/queue", json={"name": "Test", "size": 3})
        assert r.status_code == 200 and r.json()["ok"]
        qid = r.json()["id"]
        # The party is either still queued or the simulator already seated it.
        queued = any(q["id"] == qid for q in client.get("/api/queue").json())
        seated = any(h["id"] == qid for h in client.get("/api/state").json()["queue_history"])
        assert queued or seated
        assert client.post("/api/queue", json={"name": "x", "size": 0}).status_code == 422
        assert client.post("/api/tables/NOPE/mark_clean").status_code == 404
        assert client.post("/api/sim/speed", json={"speed": 20}).json()["speed"] == 20
        assert client.get("/api/analytics/weekly?weeks=1").status_code == 200
        csv = client.get("/api/analytics/export.csv?weeks=1")
        assert csv.status_code == 200 and csv.text.startswith("date,covers")
        assert client.get("/api/snapshot").headers["content-type"] == "image/jpeg"
        assert client.get("/").status_code == 200 and client.get("/analytics").status_code == 200
