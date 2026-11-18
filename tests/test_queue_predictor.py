"""Queue management and next-free predictions."""
from __future__ import annotations

import pytest

from backend.queue_predictor import (MIN_OCCUPIED_ETA_SEC, FixedStats, QueueManager, predict,
                                     time_to_free)

NOW = 1_800_000_000.0
STATS = FixedStats(session_sec=3000, cleaning_sec=180)


def table(tid: str, seats: int, state: str = "FREE", elapsed: float = 0.0,
          session_elapsed: float | None = None) -> dict:
    start = NOW - (session_elapsed if session_elapsed is not None else elapsed)
    return {"id": tid, "name": tid, "seats": seats, "state": state, "elapsed_sec": elapsed,
            "state_since": NOW - elapsed,
            "session_start": start if state in ("SEATED_WAITING", "SERVED") else None}


# ---------------------------------------------------------------- time_to_free
def test_free_table_is_available_now() -> None:
    assert time_to_free(table("T1", 4), NOW, STATS) == 0


def test_occupied_table_eta_is_avg_session_minus_elapsed_plus_cleaning() -> None:
    t = table("T1", 4, "SERVED", elapsed=600, session_elapsed=1200)
    assert time_to_free(t, NOW, STATS) == pytest.approx(3000 - 1200 + 180)


def test_overdue_table_is_clamped_to_a_couple_of_minutes() -> None:
    t = table("T1", 4, "SERVED", session_elapsed=5000)
    assert time_to_free(t, NOW, STATS) == pytest.approx(MIN_OCCUPIED_ETA_SEC + 180)


def test_cleaning_table_eta_is_remaining_cleaning_time() -> None:
    t = table("T1", 4, "NEEDS_CLEANING", elapsed=60)
    assert time_to_free(t, NOW, STATS) == pytest.approx(120)


# --------------------------------------------------------------------- predict
def q(*parties: tuple[str, int]) -> QueueManager:
    m = QueueManager()
    for name, size in parties:
        m.add(name, size, NOW - 60)
    return m


def test_predictions_exclude_free_tables_and_are_sorted() -> None:
    tables = [table("T1", 4, "SERVED", session_elapsed=2500), table("T2", 2),
              table("T3", 4, "SERVED", session_elapsed=500), table("T4", 6, "NEEDS_CLEANING", elapsed=30)]
    preds, _ = predict(tables, [], NOW, STATS)
    assert [p["table_id"] for p in preds] == ["T4", "T1", "T3"]
    assert all(p["eta_at"] == pytest.approx(NOW + p["eta_sec"], abs=1) for p in preds)


def test_smallest_free_table_that_fits_is_suggested() -> None:
    tables = [table("T6", 6), table("T4", 4), table("T2", 2)]
    _, view = predict(tables, q(("Sharma", 2)).entries(), NOW, STATS)
    assert view[0]["suggested_table"] == "T2" and view[0]["est_wait_sec"] == 0


def test_large_party_gets_six_seater() -> None:
    tables = [table("T6", 6, "SERVED", session_elapsed=2000), table("T4", 4)]
    _, view = predict(tables, q(("Big", 5)).entries(), NOW, STATS)
    assert view[0]["suggested_table"] == "T6"
    assert view[0]["est_wait_sec"] == pytest.approx(3000 - 2000 + 180, abs=1)


def test_queue_with_no_tables_configured_says_so() -> None:
    _, view = predict([], q(("Early", 2)).entries(), NOW, STATS)
    assert view[0]["suggested_table"] is None and "no tables" in view[0]["note"].lower()


def test_party_too_big_for_any_table() -> None:
    _, view = predict([table("T4", 4)], q(("Huge", 9)).entries(), NOW, STATS)
    assert view[0]["suggested_table"] is None and view[0]["est_wait_sec"] is None
    assert "combine" in view[0]["note"].lower()


def test_second_party_waits_for_next_cycle_of_the_same_table() -> None:
    tables = [table("T4", 4, "SERVED", session_elapsed=2400)]
    _, view = predict(tables, q(("A", 4), ("B", 4)).entries(), NOW, STATS)
    first = 3000 - 2400 + 180
    assert view[0]["est_wait_sec"] == pytest.approx(first, abs=1)
    assert view[1]["est_wait_sec"] == pytest.approx(first + 3000 + 180, abs=1)


def test_prefers_smaller_table_when_it_frees_almost_as_soon() -> None:
    tables = [table("T6", 6, "SERVED", session_elapsed=2900),   # ~5 min
              table("T2", 2, "SERVED", session_elapsed=2800)]   # ~6.7 min
    _, view = predict(tables, q(("Couple", 2)).entries(), NOW, STATS)
    assert view[0]["suggested_table"] == "T2"


def test_waits_for_much_sooner_bigger_table() -> None:
    tables = [table("T6", 6), table("T2", 2, "SERVED", session_elapsed=600)]
    _, view = predict(tables, q(("Couple", 2)).entries(), NOW, STATS)
    assert view[0]["suggested_table"] == "T6" and view[0]["est_wait_sec"] == 0


# ---------------------------------------------------------------- QueueManager
def test_empty_queue_is_falsy_but_must_be_passed_by_identity() -> None:
    m = QueueManager()
    assert len(m) == 0 and not m  # regression: `queue or QueueManager()` replaced the shared queue
    shared = m if m is not None else QueueManager()
    assert shared is m


def test_queue_add_sanitises_and_remove_records_history() -> None:
    m = QueueManager()
    e = m.add("   ", 0, NOW)
    assert e.name == "Guest" and e.size == 1
    assert m.remove(e.id, NOW + 90, reason="seated", table_id="T3") is e
    assert m.history[-1]["table_id"] == "T3" and m.history[-1]["waited_sec"] == 90
    assert m.remove(e.id, NOW) is None


def test_expire_drops_impatient_parties_only() -> None:
    m = QueueManager()
    m.add("patient", 2, NOW)                       # host-added: waits forever
    m.add("impatient", 2, NOW, source="sim", patience_sec=600)
    assert m.expire(NOW + 599) == 0
    assert m.expire(NOW + 600) == 1
    assert [e.name for e in m.entries()] == ["patient"]
    assert m.history[-1]["reason"] == "walked_away"
