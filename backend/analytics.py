"""Weekly analytics over the ``events`` table, plus a demo-history seeder.

The seeder writes four weeks of realistic events (same event vocabulary the engine
emits) so charts look full in demos. It models a "go-live" two weeks ago: before it,
waiter responses and cleaning were slower, which powers the before/after revenue card.
"""
from __future__ import annotations

import csv
import io
import logging
import math
import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any, Iterable

from .simulator import PARTY_SIZES, PARTY_WEIGHTS, arrival_rate_per_hour

log = logging.getLogger(__name__)

OPEN_HOUR, CLOSE_HOUR = 11.0, 23.5
LAST_SEATING_HOUR = 22.75
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
HOURS = list(range(11, 24))
WEEKDAY_FACTOR = [0.75, 0.8, 0.85, 0.95, 1.25, 1.4, 1.25]  # Mon..Sun demand multiplier
SHIFTS = [("Lunch", 11, 15), ("Afternoon", 15, 18), ("Dinner", 18, 24)]
BIN_SEC = 900  # 15-minute occupancy bins
SEED_META_KEY = "demo_seeded_at"
GO_LIVE_META_KEY = "go_live_at"


# ===================================================================== seeding
def seed_demo_history(db: Any, tables: list[dict[str, Any]], cfg: dict[str, Any], *,
                      weeks: int = 4, seed: int = 7, force: bool = False) -> int:
    """Insert ``weeks`` of fake history ending yesterday. Returns number of events written.

    Runs once (guarded by a meta key) unless ``force`` is set.
    """
    if not force and db.get_meta(SEED_META_KEY):
        return 0
    rng = random.Random(seed)
    eng = cfg.get("engine") or {}
    al = cfg.get("alerts") or {}
    seat_confirm = float(eng.get("seat_confirm_sec", 8))
    leave_confirm = float(eng.get("leave_confirm_sec", 20))
    waiter_alert = float(al.get("waiter_alert_sec", 180))
    escalate = float(al.get("escalate_sec", 300))
    clean_alert = float(al.get("clean_alert_sec", 120))

    today = date.today()
    first_day = today - timedelta(days=weeks * 7)
    go_live = datetime.combine(today - timedelta(days=14), datetime.min.time()).timestamp()
    events: list[tuple] = []
    alerts: list[tuple] = []

    def ev(tid: str, seats: int | None, etype: str, ts: float, dur: float | None,
           guests: int | None, frm: str | None = None, to: str | None = None) -> None:
        events.append((tid, seats, etype, frm, to, ts, dur, guests, "seed"))

    def alert(tid: str, kind: str, sev: str, msg: str, created: float, resolved: float) -> None:
        ack = created + rng.uniform(15, 90) if rng.random() < 0.8 else None
        if ack is not None and ack > resolved:
            ack = None
        alerts.append((tid, kind, sev, msg, created, ack, resolved, "seed"))

    tdefs = [(str(t["id"]), int(t.get("seats", 4)), str(t.get("name") or t["id"])) for t in tables]
    for day_i in range((today - first_day).days):
        day = first_day + timedelta(days=day_i)
        base = datetime.combine(day, datetime.min.time()).timestamp()
        before = base < go_live
        dow_factor = WEEKDAY_FACTOR[day.weekday()] * rng.uniform(0.9, 1.1)
        # behaviour before vs after go-live
        resp_normal, resp_slow, slow_p = ((1.5, 4.0), (3.5, 8.0), 0.35) if before else ((1.0, 3.0), (3.2, 6.0), 0.15)
        clean_normal, clean_slow, clean_slow_p = ((1.5, 3.5), (3.0, 9.0), 0.55) if before else ((1.0, 2.0), (2.0, 6.0), 0.3)

        free_at = {tid: base + OPEN_HOUR * 3600 for tid, _, _ in tdefs}
        last_free_since = dict(free_at)
        # arrivals by thinning a Poisson process
        arrivals: list[tuple[float, int, float]] = []
        t = base + OPEN_HOUR * 3600
        max_rate = 40.0 * 1.6 / 3600
        while True:
            t += rng.expovariate(max_rate)
            hour = (t - base) / 3600
            if hour > LAST_SEATING_HOUR:
                break
            if rng.random() < arrival_rate_per_hour(hour) * dow_factor / 3600 / max_rate:
                arrivals.append((t, rng.choices(PARTY_SIZES, PARTY_WEIGHTS)[0], rng.uniform(15, 45) * 60))

        queue: list[tuple[float, int, float]] = []
        ai = 0
        minute = base + OPEN_HOUR * 3600
        end = base + CLOSE_HOUR * 3600
        while minute < end:
            while ai < len(arrivals) and arrivals[ai][0] < minute + 60:
                queue.append(arrivals[ai])
                ai += 1
            still: list[tuple[float, int, float]] = []
            for arr, size, patience in queue:
                if minute - arr > patience:
                    ev("-", None, "WALKAWAY", arr + patience, patience, size)
                    continue
                fits = [(s, tid) for tid, s, _ in tdefs if s >= size and free_at[tid] <= minute]
                if not fits:
                    still.append((arr, size, patience))
                    continue
                seats, tid = min(fits)
                seat = max(arr, minute) + rng.uniform(0, 50)
                resp = rng.uniform(*(resp_slow if rng.random() < slow_p else resp_normal)) * 60
                dining = rng.uniform(35, 75) * 60 * (0.9 + 0.04 * size)
                served = seat + resp
                leave = served + dining
                cdelay = rng.uniform(*(clean_slow if rng.random() < clean_slow_p else clean_normal)) * 60
                guests = size if rng.random() > 0.1 else max(1, size - 1)
                ev(tid, seats, "SEATED", seat + seat_confirm, seat - last_free_since[tid], guests, "FREE", "SEATED_WAITING")
                name = next(n for i, s, n in tdefs if i == tid)
                if resp >= waiter_alert:
                    ev(tid, seats, "ALERT_WAITER", seat + waiter_alert, waiter_alert, None)
                    sev = "critical" if resp >= escalate else "warning"
                    if sev == "critical":
                        ev(tid, seats, "ALERT_ESCALATED", seat + escalate, escalate, None)
                    alert(tid, "waiter", sev, f"Table {name} waiting {int(resp // 60)} min with no waiter",
                          seat + waiter_alert, served + 3)
                ev(tid, seats, "SERVED", served + 3, resp, guests, "SEATED_WAITING", "SERVED")
                ev(tid, seats, "LEFT", leave + leave_confirm, leave - seat, guests, "SERVED", "NEEDS_CLEANING")
                if cdelay >= clean_alert:
                    ev(tid, seats, "ALERT_CLEANING", leave + clean_alert, clean_alert, None)
                    alert(tid, "cleaning", "warning", f"Table {name} needs cleaning",
                          leave + clean_alert, leave + cdelay + 5)
                ev(tid, seats, "CLEANED", leave + cdelay + 5, cdelay, 0, "NEEDS_CLEANING", "FREE")
                free_at[tid] = leave + cdelay + rng.uniform(60, 150)
                last_free_since[tid] = free_at[tid]
            queue = still
            minute += 60
        for arr, size, _patience in queue:  # still waiting at close
            ev("-", None, "WALKAWAY", end, end - arr, size)

    db.executemany(
        "INSERT INTO events (table_id, seats, event_type, from_state, to_state, timestamp,"
        " duration_sec, guest_count, source) VALUES (?,?,?,?,?,?,?,?,?)", events)
    db.executemany(
        "INSERT INTO alerts (table_id, type, severity, message, created_at, acknowledged_at,"
        " resolved_at, source) VALUES (?,?,?,?,?,?,?,?)", alerts)
    db.set_meta(SEED_META_KEY, datetime.now().isoformat(timespec="seconds"))
    if not db.get_meta(GO_LIVE_META_KEY):
        db.set_meta(GO_LIVE_META_KEY, str(go_live))
    log.info("Seeded %d demo events and %d alerts (%d weeks)", len(events), len(alerts), weeks)
    return len(events)


# ==================================================================== analytics
def _range(weeks: int, now: float | None = None) -> tuple[float, float]:
    """[start, end): the last ``weeks``×7 *complete* days, ending at midnight today.

    Today is excluded so a half-finished day never drags the daily averages down.
    """
    today = datetime.fromtimestamp(now).date() if now else date.today()
    end = datetime.combine(today, datetime.min.time()).timestamp()
    start = end - weeks * 7 * 86400
    return start, end


def _avg(xs: Iterable[float | None]) -> float | None:
    vals = [x for x in xs if x is not None]
    return mean(vals) if vals else None


def _r(x: float | None, nd: int = 1) -> float | None:
    return None if x is None else round(x, nd)


def weekly_report(db: Any, tables: list[dict[str, Any]], cfg: dict[str, Any], *,
                  weeks: int = 1, now: float | None = None) -> dict[str, Any]:
    """Everything the analytics page needs, for the last ``weeks`` weeks."""
    weeks = max(1, min(12, int(weeks)))
    start, end = _range(weeks, now)
    rows = db.query("SELECT table_id, seats, event_type, timestamp, duration_sec, guest_count"
                    " FROM events WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                    (start, end))
    an = cfg.get("analytics") or {}
    tables_per_waiter = int(an.get("tables_per_waiter", 4))
    avg_bill = float(an.get("avg_bill", 600))
    currency = (cfg.get("restaurant") or {}).get("currency", "₹")
    n_days = round((end - start) / 86400)
    dates = [(datetime.fromtimestamp(start) + timedelta(days=i)).date() for i in range(n_days)]

    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_type[r["event_type"]].append(r)

    def day_of(ts: float) -> date:
        return datetime.fromtimestamp(ts).date()

    # ---- footfall: covers by seat time
    heat = [[0.0] * len(HOURS) for _ in DAYS]
    day_count = defaultdict(int)
    for d in dates:
        day_count[d.weekday()] += 1
    covers_by_hour = defaultdict(float)
    covers_by_day: dict[date, int] = defaultdict(int)
    for r in by_type["SEATED"]:
        dt = datetime.fromtimestamp(r["timestamp"])
        g = r["guest_count"] or 0
        covers_by_day[dt.date()] += g
        if dt.hour in HOURS:
            heat[dt.weekday()][HOURS.index(dt.hour)] += g
            covers_by_hour[dt.hour] += g
    heat = [[round(v / max(1, day_count[i]), 1) for v in row] for i, row in enumerate(heat)]
    peak = [round(covers_by_hour[h] / max(1, n_days), 1) for h in HOURS]

    # ---- per-day series
    def per_day(etype: str) -> dict[date, list[float]]:
        out: dict[date, list[float]] = defaultdict(list)
        for r in by_type[etype]:
            if r["duration_sec"] is not None:
                out[day_of(r["timestamp"])].append(r["duration_sec"])
        return out

    sessions, responses = per_day("LEFT"), per_day("SERVED")
    cleans = per_day("CLEANED")
    for k, v in per_day("MANUAL_CLEAN").items():
        cleans[k].extend(v)
    alerts_w = defaultdict(int)
    alerts_c = defaultdict(int)
    for r in by_type["ALERT_WAITER"]:
        alerts_w[day_of(r["timestamp"])] += 1
    for r in by_type["ALERT_CLEANING"]:
        alerts_c[day_of(r["timestamp"])] += 1

    def m(series: dict[date, list[float]], d: date) -> float | None:
        a = _avg(series.get(d, []))
        return None if a is None else a / 60

    turn_day = []
    for d in dates:
        s, c = m(sessions, d), m(cleans, d)
        turn_day.append(_r(s + (c or 0)) if s is not None else None)
    daily = {
        "dates": [d.isoformat() for d in dates],
        "labels": [f"{DAYS[d.weekday()]} {d.day}" for d in dates],
        "turn_min": turn_day,
        "response_min": [_r(m(responses, d)) for d in dates],
        "cleaning_min": [_r(m(cleans, d)) for d in dates],
        "alerts_waiter": [alerts_w[d] for d in dates],
        "alerts_cleaning": [alerts_c[d] for d in dates],
        "covers": [covers_by_day[d] for d in dates],
    }

    # ---- utilisation per table (occupied time / open time)
    open_sec = (CLOSE_HOUR - OPEN_HOUR) * 3600 * n_days
    occ = defaultdict(float)
    intervals: list[tuple[float, float]] = []
    for r in by_type["LEFT"]:
        if r["duration_sec"]:
            occ[r["table_id"]] += r["duration_sec"]
            intervals.append((r["timestamp"] - r["duration_sec"], r["timestamp"]))
    util_tables = [{"id": t["id"], "name": t.get("name", t["id"]), "seats": t.get("seats"),
                    "pct": round(100 * min(1.0, occ[t["id"]] / open_sec), 1)} for t in tables]
    util_overall = round(mean(t["pct"] for t in util_tables), 1) if util_tables else 0.0

    # ---- staffing: peak concurrent occupied tables per shift
    # occupied-table count per 15-minute bin (keyed by local day + bin start)
    quarter: dict[tuple[date, float], int] = defaultdict(int)
    for a, b in intervals:
        # A session counts toward a bin if it covers the bin's midpoint, so a table
        # turning over inside one bin is never counted twice.
        k = math.ceil((a - BIN_SEC / 2) / BIN_SEC)
        while k * BIN_SEC + BIN_SEC / 2 < b:
            quarter[(day_of(k * BIN_SEC), k * BIN_SEC)] += 1
            k += 1
    staffing = []
    for name, h0, h1 in SHIFTS:
        peaks = {"weekday": [], "weekend": []}
        for d in dates:
            ds = datetime.combine(d, datetime.min.time()).timestamp()
            vals = [quarter.get((d, ds + q * BIN_SEC), 0)
                    for q in range(int(h0 * 4), int(h1 * 4))]
            if any(vals):
                # typical peak load = 90th-percentile 15-min bin (one stray busy bin
                # at a shift boundary shouldn't set the whole shift's staffing)
                vals.sort()
                peaks["weekend" if d.weekday() >= 4 else "weekday"].append(vals[int(0.9 * (len(vals) - 1))])
        row: dict[str, Any] = {"shift": name, "hours": f"{h0:02d}:00–{h1 % 24:02d}:00"}
        for k, v in peaks.items():
            p = round(mean(v), 1) if v else 0
            row[f"{k}_peak_tables"] = p
            row[f"{k}_waiters"] = max(1, math.ceil(p / tables_per_waiter)) if p else 0
        staffing.append(row)

    # ---- KPIs
    total_covers = sum(r["guest_count"] or 0 for r in by_type["SEATED"])
    kpis = {
        "covers": total_covers,
        "parties": len(by_type["SEATED"]),
        "covers_per_day": round(total_covers / max(1, n_days)),
        "avg_turn_min": _r(_avg(x for x in turn_day if x is not None)),
        "avg_response_min": _r(_avg(r["duration_sec"] / 60 for r in by_type["SERVED"] if r["duration_sec"] is not None)),
        "avg_cleaning_min": _r(_avg(r["duration_sec"] / 60 for r in by_type["CLEANED"] + by_type["MANUAL_CLEAN"]
                                    if r["duration_sec"] is not None)),
        "alerts": len(by_type["ALERT_WAITER"]) + len(by_type["ALERT_CLEANING"]),
        "waiter_alerts": len(by_type["ALERT_WAITER"]),
        "cleaning_alerts": len(by_type["ALERT_CLEANING"]),
        "utilisation_pct": util_overall,
        "walkaways": len(by_type["WALKAWAY"]),
        "walkaway_covers": sum(r["guest_count"] or 0 for r in by_type["WALKAWAY"]),
    }

    return {
        "range": {"start": start, "end": end, "weeks": weeks, "days": n_days},
        "currency": currency,
        "kpis": kpis,
        "heatmap": {"days": DAYS, "hours": HOURS, "values": heat},
        "peak_hours": {"hours": HOURS, "avg_covers": peak},
        "daily": daily,
        "utilisation": {"tables": util_tables, "overall": util_overall},
        "staffing": {"tables_per_waiter": tables_per_waiter, "shifts": staffing},
        "revenue": revenue_impact(db, avg_bill=avg_bill, currency=currency, now=now),
    }


def revenue_impact(db: Any, *, avg_bill: float, currency: str = "₹",
                   now: float | None = None) -> dict[str, Any]:
    """Before/after comparison around go-live and the extra covers freed by faster turns.

    extra turns/week = peak-hour sessions/week × (turn_before / turn_after − 1)
    extra covers/week = extra turns × avg party size;  revenue = covers × avg bill
    Capped by covers actually lost to walk-aways (you can't seat guests who never came).
    """
    go_live_raw = db.get_meta(GO_LIVE_META_KEY)
    end = _range(1, now)[1]
    if go_live_raw:
        go_live = float(go_live_raw)
    else:
        first = db.query("SELECT MIN(timestamp) AS t FROM events")[0]["t"]
        if first is None:
            return {"available": False}
        go_live = first + (end - first) / 2
    start = go_live - 14 * 86400
    rows = db.query("SELECT event_type, timestamp, duration_sec, guest_count FROM events"
                    " WHERE timestamp >= ? AND timestamp < ? AND event_type IN"
                    " ('SEATED','SERVED','LEFT','CLEANED','MANUAL_CLEAN','WALKAWAY','ALERT_WAITER')",
                    (start, end))

    def period(lo: float, hi: float) -> dict[str, Any]:
        sel = [r for r in rows if lo <= r["timestamp"] < hi]
        weeks = max(1e-9, (hi - lo) / (7 * 86400))
        get = lambda t: [r for r in sel if r["event_type"] == t]  # noqa: E731
        session = _avg(r["duration_sec"] for r in get("LEFT"))
        clean = _avg(r["duration_sec"] for r in get("CLEANED") + get("MANUAL_CLEAN"))
        resp = _avg(r["duration_sec"] for r in get("SERVED"))
        seated = get("SEATED")
        peak_sessions = [r for r in seated
                         if datetime.fromtimestamp(r["timestamp"]).hour in (12, 13, 14, 19, 20, 21)]
        return {
            "turn_min": _r(((session or 0) + (clean or 0)) / 60) if session else None,
            "response_min": _r(resp / 60) if resp else None,
            "cleaning_min": _r(clean / 60) if clean else None,
            "covers_per_week": round(sum(r["guest_count"] or 0 for r in seated) / weeks),
            "peak_sessions_per_week": len(peak_sessions) / weeks,
            "avg_party": _avg(r["guest_count"] for r in seated) or 2.5,
            "walkaway_covers_per_week": sum(r["guest_count"] or 0 for r in get("WALKAWAY")) / weeks,
            "waiter_alerts_per_week": round(len(get("ALERT_WAITER")) / weeks),
        }

    before, after = period(start, go_live), period(go_live, end)
    if not before["turn_min"] or not after["turn_min"]:
        return {"available": False}
    gain = max(0.0, before["turn_min"] / after["turn_min"] - 1)
    extra_turns = after["peak_sessions_per_week"] * gain
    extra_covers = extra_turns * after["avg_party"]
    extra_covers = min(extra_covers, before["walkaway_covers_per_week"] or extra_covers)
    weekly = extra_covers * avg_bill
    clean_keys = ("turn_min", "response_min", "cleaning_min", "covers_per_week", "waiter_alerts_per_week")
    return {
        "available": True,
        "go_live": go_live,
        "avg_bill": avg_bill,
        "currency": currency,
        "before": {k: before[k] for k in clean_keys},
        "after": {k: after[k] for k in clean_keys},
        "turn_saved_min": _r(before["turn_min"] - after["turn_min"]),
        "extra_covers_per_week": round(extra_covers),
        "extra_revenue_per_week": round(weekly, -2),
        "extra_revenue_per_month": round(weekly * 52 / 12, -2),
    }


# ======================================================================= export
def export_csv(db: Any, tables: list[dict[str, Any]], cfg: dict[str, Any], *,
               weeks: int = 1, kind: str = "daily", now: float | None = None) -> str:
    """CSV text: ``daily`` summary rows or raw ``events`` rows for the selected range."""
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "events":
        start, end = _range(max(1, min(12, int(weeks))), now)
        rows = db.query("SELECT timestamp, table_id, seats, event_type, from_state, to_state,"
                        " duration_sec, guest_count, source FROM events"
                        " WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp", (start, end))
        w.writerow(["datetime", "table", "seats", "event", "from_state", "to_state",
                    "duration_min", "guests", "source"])
        for r in rows:
            w.writerow([datetime.fromtimestamp(r["timestamp"]).isoformat(sep=" ", timespec="seconds"),
                        r["table_id"], r["seats"], r["event_type"], r["from_state"], r["to_state"],
                        "" if r["duration_sec"] is None else round(r["duration_sec"] / 60, 2),
                        r["guest_count"], r["source"]])
        return buf.getvalue()

    rep = weekly_report(db, tables, cfg, weeks=weeks, now=now)
    d = rep["daily"]
    w.writerow(["date", "covers", "avg_turn_min", "avg_waiter_response_min",
                "avg_cleaning_delay_min", "waiter_alerts", "cleaning_alerts"])
    for i, day in enumerate(d["dates"]):
        w.writerow([day, d["covers"][i], d["turn_min"][i], d["response_min"][i],
                    d["cleaning_min"][i], d["alerts_waiter"][i], d["alerts_cleaning"][i]])
    return buf.getvalue()
