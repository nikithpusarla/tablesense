"""Simulation mode: a virtual 16-table restaurant that needs no camera and no AI model.

The simulator keeps *ground truth* (which party sits where, where each waiter is) and
converts it into noisy per-table :class:`Observation` readings — exactly what the live
detector produces — which are fed into the shared :class:`TableEngine`. The dashboard
therefore shows the engine's *perception*, debouncing and alerts behave as in live mode.

It also renders a top-down animated floor plan used as the ``/video_feed`` in this mode.
"""
from __future__ import annotations

import logging
import math
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import cv2
import numpy as np

from .queue_predictor import QueueManager
from .table_engine import Observation, TableEngine

log = logging.getLogger(__name__)

# Floor canvas (pixels) used both for table polygons and the rendered frame.
FLOOR_W, FLOOR_H = 1120, 680
HEADER_H = 44
ENTRANCE_X = 90
STATION_X = 1035
WAITER_SPEED = 140.0  # px per sim-second

STATE_BGR: dict[str, tuple[int, int, int]] = {
    "FREE": (94, 197, 34),
    "SEATED_WAITING": (68, 68, 239),
    "SERVED": (22, 115, 249),
    "NEEDS_CLEANING": (8, 179, 234),
}
GUEST_BGR = (246, 130, 59)
STAFF_BGR = (247, 85, 168)

TASK_PRIORITY = {"serve": 0, "check": 1, "clean": 2}
PARTY_SIZES = [1, 2, 3, 4, 5, 6]
SURNAMES = ["Sharma", "Iyer", "Khan", "Reddy", "Patel", "Das", "Mehta", "Nair", "Singh",
            "Gupta", "Rao", "Kapoor", "Joshi", "Menon", "Bose", "Fernandes", "Verma", "Pillai"]
PARTY_WEIGHTS = [8, 40, 14, 26, 6, 6]


# --------------------------------------------------------------------------- model
@dataclass
class Party:
    id: int
    size: int
    arrived_at: float
    patience_sec: float
    seated_at: Optional[float] = None
    served_at: Optional[float] = None
    leave_at: Optional[float] = None


@dataclass
class SimTable:
    id: str
    name: str
    seats: int
    cx: float
    cy: float
    w: float
    h: float
    phase: str = "clean"  # clean | occupied | dirty
    party: Optional[Party] = None

    @property
    def polygon(self) -> list[list[float]]:
        x0, y0 = self.cx - self.w / 2, self.cy - self.h / 2
        x1, y1 = self.cx + self.w / 2, self.cy + self.h / 2
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]

    @property
    def staff_spot(self) -> tuple[float, float]:
        """Where a waiter stands when attending this table (right-hand side)."""
        return self.cx + self.w / 2 + 22, self.cy

    def seat_positions(self) -> list[tuple[float, float]]:
        rx, ry = self.w / 2 + 13, self.h / 2 + 13
        out = []
        for i in range(self.seats):
            a = math.pi + (2 * math.pi * i / self.seats) + (math.pi / self.seats if self.seats == 4 else 0)
            out.append((self.cx + rx * math.cos(a), self.cy + ry * math.sin(a)))
        return out


@dataclass
class Task:
    table_id: str
    kind: str  # serve | check | clean
    due_at: float
    duration: float
    party_id: Optional[int] = None


@dataclass
class Waiter:
    id: int
    home: tuple[float, float]
    pos: tuple[float, float]
    state: str = "idle"  # idle | walking | at_table | returning
    task: Optional[Task] = None
    src: tuple[float, float] = (0.0, 0.0)
    dst: tuple[float, float] = (0.0, 0.0)
    depart_at: float = 0.0
    arrive_at: float = 0.0
    busy_until: float = 0.0
    phase_offset: float = field(default_factory=lambda: random.random() * 10)


def _lerp(a: tuple[float, float], b: tuple[float, float], t: float) -> tuple[float, float]:
    t = min(1.0, max(0.0, t))
    return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t


def _bump(h: float, start: float, end: float, ramp: float = 0.75) -> float:
    """Smooth trapezoid: 0 outside, 1 inside [start, end], cosine ramps of ``ramp`` hours."""
    if h < start - ramp or h > end + ramp:
        return 0.0
    if h < start:
        return 0.5 - 0.5 * math.cos(math.pi * (h - (start - ramp)) / ramp)
    if h > end:
        return 0.5 + 0.5 * math.cos(math.pi * (h - end) / ramp)
    return 1.0


def arrival_rate_per_hour(hour: float) -> float:
    """Expected walk-in parties per hour for a given (fractional) hour of day."""
    if hour < 11.0 or hour > 23.5:
        return 0.0
    return 2.0 + 15.0 * _bump(hour, 12.25, 14.75) + 21.0 * _bump(hour, 19.25, 22.0) + 3.0 * _bump(hour, 17.5, 18.75, 0.5)


def build_layout() -> list[SimTable]:
    """16 tables: 4×2-seat, 8×4-seat, 4×6-seat on a 4×4 grid."""
    tables: list[SimTable] = []
    x0, x1 = 200, 900
    y0, y1 = HEADER_H + 62, FLOOR_H - 72
    cols, rows = 4, 4
    cw, rh = (x1 - x0) / (cols - 1), (y1 - y0) / (rows - 1)
    seats_by_row = [2, 4, 4, 6]
    size = {2: (64, 64), 4: (84, 84), 6: (132, 76)}
    n = 1
    for r in range(rows):
        for c in range(cols):
            seats = seats_by_row[r]
            w, h = size[seats]
            tables.append(SimTable(id=f"T{n}", name=f"T{n}", seats=seats,
                                   cx=x0 + c * cw, cy=y0 + r * rh, w=w, h=h))
            n += 1
    return tables


# ----------------------------------------------------------------------- simulator
class Simulator:
    """Runs the virtual restaurant on its own (speed-scaled) clock in a background thread."""

    MAX_STEP = 1.0  # sim seconds per engine update

    def __init__(self, cfg: dict[str, Any], queue: QueueManager | None = None) -> None:
        sim_cfg = cfg.get("simulation") or {}
        self.cfg = sim_cfg
        seed = sim_cfg.get("seed")
        self.rng = random.Random(seed)
        self.speed: float = float(sim_cfg.get("speed", 5))
        self.warmup_sec: float = float(sim_cfg.get("warmup_min", 0)) * 60
        # Start earlier by the warm-up period so the floor is busy when the demo opens.
        self.sim_time: float = self._start_time(str(sim_cfg.get("start_time", "19:00"))) - self.warmup_sec
        noise = sim_cfg.get("noise") or {}
        self.miss_prob = float(noise.get("miss_prob", 0.04))
        self.ghost_prob = float(noise.get("ghost_prob", 0.005))
        self.resp_range = sim_cfg.get("waiter_response_min", [1, 3])
        self.resp_slow_range = sim_cfg.get("waiter_response_slow_min", [3.2, 6])
        self.slow_prob = float(sim_cfg.get("slow_waiter_prob", 0.15))
        self.dining_range = sim_cfg.get("dining_min", [35, 75])
        self.clean_range = sim_cfg.get("cleaning_delay_min", [1, 2])
        self.clean_slow_range = sim_cfg.get("cleaning_delay_slow_min", [2, 6])
        self.clean_slow_prob = float(sim_cfg.get("slow_cleaning_prob", 0.3))

        self.tables: dict[str, SimTable] = {t.id: t for t in build_layout()}
        n_waiters = int(sim_cfg.get("num_waiters", 4))
        self.waiters = []
        for i in range(n_waiters):
            home = (STATION_X, HEADER_H + 120 + i * 44)
            self.waiters.append(Waiter(id=i + 1, home=home, pos=home))
        # Entrance queue is the shared host queue: sim walk-ins appear on the dashboard,
        # and parties the host adds manually get seated by the simulation.
        self.queue = queue if queue is not None else QueueManager()
        self.tasks: list[Task] = []
        self.stats = {"arrivals": 0, "seated": 0, "walkaways": 0}

        self.engine: TableEngine | None = None
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._running = False

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _start_time(spec: str) -> float:
        if spec.lower() == "now":
            return time.time()
        try:
            hh, mm = (int(x) for x in spec.split(":"))
        except ValueError:
            log.warning("Invalid simulation.start_time %r, using 18:00", spec)
            hh, mm = 18, 0
        return datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()

    def layout(self) -> list[dict[str, Any]]:
        """Table definitions (id, name, seats, polygon) for building the engine."""
        return [{"id": t.id, "name": t.name, "seats": t.seats, "polygon": t.polygon}
                for t in self.tables.values()]

    def attach(self, engine: TableEngine) -> None:
        self.engine = engine

    def now(self) -> float:
        with self._lock:
            return self.sim_time

    def set_speed(self, speed: float) -> float:
        with self._lock:
            self.speed = float(min(120.0, max(0.25, speed)))
            log.info("Simulation speed set to %sx", self.speed)
            return self.speed

    def warmup(self) -> None:
        """Fast-forward through the warm-up period (no real-time wait)."""
        if self.warmup_sec > 0:
            log.info("Warming up simulation: %.0f sim minutes", self.warmup_sec / 60)
            self.advance(self.warmup_sec)
            self.warmup_sec = 0

    # --------------------------------------------------------------- threading
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="simulator", daemon=True)
        self._thread.start()
        log.info("Simulator started at %s, speed %sx",
                 datetime.fromtimestamp(self.sim_time).strftime("%H:%M"), self.speed)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        last = time.monotonic()
        while self._running:
            time.sleep(0.1)
            nowm = time.monotonic()
            real_dt, last = nowm - last, nowm
            try:
                self.advance(real_dt * self.speed)
            except Exception:
                log.exception("Simulator step failed")

    def advance(self, sim_dt: float) -> None:
        """Advance the simulation by ``sim_dt`` seconds in steps of at most MAX_STEP."""
        remaining = sim_dt
        while remaining > 1e-9:
            dt = min(self.MAX_STEP, remaining)
            remaining -= dt
            with self._lock:
                self.sim_time += dt
                self._step(dt)
                obs = self._observe()
                now = self.sim_time
            if self.engine is not None:
                self.engine.update(now, obs)

    # -------------------------------------------------------------- dynamics
    def _hour(self) -> float:
        d = datetime.fromtimestamp(self.sim_time)
        return d.hour + d.minute / 60 + d.second / 3600

    def _minutes(self, rng_pair: list[float]) -> float:
        """Uniform random duration in seconds from a [min, max] minute range."""
        return self.rng.uniform(float(rng_pair[0]), float(rng_pair[1])) * 60

    def _delay(self, normal: list[float], slow: list[float], slow_prob: float) -> float:
        """Mostly-normal delay with an occasional slow outlier (these trigger alerts)."""
        return self._minutes(slow if self.rng.random() < slow_prob else normal)

    def _step(self, dt: float) -> None:
        now = self.sim_time
        # 1) arrivals (Poisson thinning per step)
        if self.rng.random() < arrival_rate_per_hour(self._hour()) / 3600.0 * dt:
            size = self.rng.choices(PARTY_SIZES, PARTY_WEIGHTS)[0]
            self.queue.add(self.rng.choice(SURNAMES), size, now, source="sim",
                           patience_sec=self.rng.uniform(15, 45) * 60)
            self.stats["arrivals"] += 1

        # 2) walk-aways from the entrance queue
        self.stats["walkaways"] += self.queue.expire(now)

        # 3) seat parties (host seats any party that fits, smallest table first)
        for entry in self.queue.entries():
            table = self._best_table(entry.size)
            if table is None:
                continue
            self.queue.remove(entry.id, now, reason="seated", table_id=table.id)
            party = Party(id=entry.id, size=entry.size, arrived_at=entry.added_at,
                          patience_sec=entry.patience_sec or math.inf, seated_at=now)
            table.party, table.phase = party, "occupied"
            self.stats["seated"] += 1
            delay = self._delay(self.resp_range, self.resp_slow_range, self.slow_prob)
            self._add_task(Task(table.id, "serve", now + delay, self.rng.uniform(40, 90), party.id))

        # 4) parties finishing their meal
        for table in self.tables.values():
            p = table.party
            if p and p.leave_at is not None and now >= p.leave_at:
                table.party, table.phase = None, "dirty"
                self.tasks = [t for t in self.tasks if t.table_id != table.id]
                self._add_task(Task(table.id, "clean", now + self._delay(self.clean_range, self.clean_slow_range,
                                                          self.clean_slow_prob),
                                    self.rng.uniform(45, 120)))

        # 5) waiters
        self._update_waiters(now)

    def _best_table(self, size: int) -> SimTable | None:
        fits = [t for t in self.tables.values() if t.phase == "clean" and t.seats >= size]
        return min(fits, key=lambda t: (t.seats, int(t.id[1:]))) if fits else None

    def _add_task(self, task: Task) -> None:
        self.tasks.append(task)
        self.tasks.sort(key=lambda t: t.due_at)

    def _update_waiters(self, now: float) -> None:
        for w in self.waiters:
            if w.state in ("walking", "returning"):
                frac = (now - w.depart_at) / max(1e-6, w.arrive_at - w.depart_at)
                w.pos = _lerp(w.src, w.dst, frac)
                if frac >= 1.0:
                    if w.state == "walking" and w.task:
                        w.state, w.busy_until = "at_table", now + w.task.duration
                    else:
                        w.state = "idle"
            elif w.state == "at_table" and now >= w.busy_until:
                self._complete_task(w, now)
                self._send(w, w.home, now, "returning")

            if w.state in ("idle", "returning"):
                # Greeting new guests takes priority over check-ins and cleaning.
                due = [t for t in self.tasks if t.due_at <= now]
                task = min(due, key=lambda t: (TASK_PRIORITY[t.kind], t.due_at)) if due else None
                if task is not None:
                    self.tasks.remove(task)
                    w.task = task
                    self._send(w, self.tables[task.table_id].staff_spot, now, "walking")

    def _send(self, w: Waiter, dst: tuple[float, float], now: float, state: str) -> None:
        dist = math.dist(w.pos, dst)
        w.src, w.dst, w.state = w.pos, dst, state
        w.depart_at, w.arrive_at = now, now + dist / WAITER_SPEED

    def _complete_task(self, w: Waiter, now: float) -> None:
        task, w.task = w.task, None
        if task is None:
            return
        table = self.tables[task.table_id]
        if task.kind == "serve" and table.party and table.party.id == task.party_id:
            p = table.party
            p.served_at = now
            p.leave_at = now + self._minutes(self.dining_range) * (0.9 + 0.04 * p.size)
            # A couple of check-in visits (food, bill) during the meal.
            for frac in sorted(self.rng.uniform(0.2, 0.95) for _ in range(self.rng.randint(1, 3))):
                self._add_task(Task(table.id, "check", now + frac * (p.leave_at - now),
                                    self.rng.uniform(15, 40), p.id))
        elif task.kind == "clean" and table.phase == "dirty":
            table.phase = "clean"

    # ----------------------------------------------------------- observation
    def _observe(self) -> dict[str, Observation]:
        obs: dict[str, Observation] = {}
        for t in self.tables.values():
            guests = 0
            if t.party:
                guests = sum(1 for _ in range(t.party.size) if self.rng.random() > self.miss_prob)
            elif self.rng.random() < self.ghost_prob:
                guests = 1
            staff = sum(1 for w in self.waiters
                        if w.state == "at_table" and w.task and w.task.table_id == t.id)
            obs[t.id] = Observation(guests=guests, staff=staff)
        return obs

    # -------------------------------------------------------------- external
    def on_manual_clean(self, table_id: str) -> None:
        """Dashboard "mark clean": a busboy cleaned it, so ground truth becomes clean too."""
        with self._lock:
            t = self.tables.get(table_id)
            if t and t.phase == "dirty":
                t.phase = "clean"
                self.tasks = [k for k in self.tasks if not (k.table_id == table_id and k.kind == "clean")]

    def public_state(self) -> dict[str, Any]:
        with self._lock:
            return {
                "speed": self.speed,
                "clock": self.sim_time,
                "queue_length": len(self.queue),
                "waiters": [{"id": w.id, "state": w.state,
                             "table_id": w.task.table_id if w.task else None} for w in self.waiters],
                "stats": dict(self.stats),
            }

    # -------------------------------------------------------------- rendering
    def render_frame(self, snapshot: dict[str, Any]) -> np.ndarray:
        """Draw the top-down floor plan coloured by the engine's perceived table states."""
        with self._lock:
            tables = list(self.tables.values())
            waiters = [(w.pos, w.state) for w in self.waiters]
            queue = [e.size for e in self.queue.entries()]
            now, speed = self.sim_time, self.speed

        img = np.full((FLOOR_H, FLOOR_W, 3), (32, 23, 15), np.uint8)
        # floor tiles
        for x in range(0, FLOOR_W, 40):
            cv2.line(img, (x, HEADER_H), (x, FLOOR_H), (40, 30, 22), 1)
        for y in range(HEADER_H, FLOOR_H, 40):
            cv2.line(img, (0, y), (FLOOR_W, y), (40, 30, 22), 1)
        # entrance + service station
        cv2.rectangle(img, (0, HEADER_H), (ENTRANCE_X + 30, FLOOR_H), (52, 38, 28), -1)
        cv2.rectangle(img, (STATION_X - 45, HEADER_H), (FLOOR_W, FLOOR_H), (52, 38, 28), -1)
        _text(img, "ENTRANCE", (14, HEADER_H + 26), 0.5, (148, 163, 184))
        _text(img, "SERVICE", (STATION_X - 32, HEADER_H + 26), 0.5, (148, 163, 184))
        cv2.rectangle(img, (STATION_X - 30, FLOOR_H - 120), (FLOOR_W - 15, FLOOR_H - 20), (70, 55, 45), -1)
        _text(img, "KITCHEN", (STATION_X - 20, FLOOR_H - 64), 0.5, (203, 213, 225))

        by_id = {t["id"]: t for t in snapshot.get("tables", [])}
        alert_tables = {a["table_id"]: a for a in snapshot.get("alerts", [])}
        pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 6)

        for t in tables:
            info = by_id.get(t.id, {})
            state = info.get("state", "FREE")
            color = STATE_BGR.get(state, (120, 120, 120))
            p0 = (int(t.cx - t.w / 2), int(t.cy - t.h / 2))
            p1 = (int(t.cx + t.w / 2), int(t.cy + t.h / 2))
            overlay = img.copy()
            cv2.rectangle(overlay, p0, p1, color, -1)
            cv2.addWeighted(overlay, 0.35, img, 0.65, 0, img)
            thick = 2
            alert = alert_tables.get(t.id)
            if alert and alert["severity"] == "critical":
                thick = 2 + int(3 * pulse)
            cv2.rectangle(img, p0, p1, color, thick, cv2.LINE_AA)
            # chairs
            for sx, sy in t.seat_positions():
                cv2.circle(img, (int(sx), int(sy)), 9, (71, 85, 105), 1, cv2.LINE_AA)
            # guests (ground-truth seating)
            if t.party:
                for sx, sy in t.seat_positions()[: t.party.size]:
                    cv2.circle(img, (int(sx), int(sy)), 8, GUEST_BGR, -1, cv2.LINE_AA)
            elapsed = int(info.get("elapsed_sec", 0))
            _text(img, t.name, (int(t.cx) - 14, int(t.cy) - 8), 0.55, (255, 255, 255), 2)
            _text(img, f"{elapsed // 60:02d}:{elapsed % 60:02d}", (int(t.cx) - 22, int(t.cy) + 14),
                  0.5, (226, 232, 240))
            if alert:
                cv2.circle(img, (p1[0] - 6, p0[1] + 6), 7, (0, 0, 255) if alert["type"] == "waiter"
                           else (0, 215, 255), -1, cv2.LINE_AA)

        for pos, _state in waiters:
            x, y = int(pos[0]), int(pos[1])
            cv2.circle(img, (x, y), 10, STAFF_BGR, -1, cv2.LINE_AA)
            cv2.circle(img, (x, y), 12, (255, 255, 255), 1, cv2.LINE_AA)
            _text(img, "W", (x - 6, y + 5), 0.42, (255, 255, 255), 1)

        # entrance queue
        for i, size in enumerate(queue[:12]):
            x, y = 30 + (i % 2) * 40, HEADER_H + 70 + (i // 2) * 44
            cv2.circle(img, (x, y), 13, GUEST_BGR, -1, cv2.LINE_AA)
            _text(img, str(size), (x - 5, y + 5), 0.45, (255, 255, 255), 1)
        if len(queue) > 12:
            _text(img, f"+{len(queue) - 12}", (30, HEADER_H + 70 + 6 * 44), 0.5, (226, 232, 240))
        _text(img, f"Queue: {len(queue)}", (14, FLOOR_H - 20), 0.5, (226, 232, 240))

        # header bar
        cv2.rectangle(img, (0, 0), (FLOOR_W, HEADER_H), (23, 15, 10), -1)
        clock = datetime.fromtimestamp(now).strftime("%a %H:%M:%S")
        _text(img, "SIMULATION", (14, 29), 0.65, (8, 179, 234), 2)
        _text(img, f"{clock}   speed {speed:g}x", (170, 29), 0.6, (226, 232, 240), 1)
        lx = 560
        for label, key in (("Free", "FREE"), ("Waiting", "SEATED_WAITING"),
                           ("Served", "SERVED"), ("Cleaning", "NEEDS_CLEANING")):
            cv2.circle(img, (lx, 22), 7, STATE_BGR[key], -1, cv2.LINE_AA)
            _text(img, label, (lx + 12, 28), 0.5, (203, 213, 225))
            lx += 105
        cv2.circle(img, (lx, 22), 7, GUEST_BGR, -1, cv2.LINE_AA)
        _text(img, "Guest", (lx + 12, 28), 0.5, (203, 213, 225))
        cv2.circle(img, (lx + 85, 22), 7, STAFF_BGR, -1, cv2.LINE_AA)
        _text(img, "Staff", (lx + 97, 28), 0.5, (203, 213, 225))
        return img


def _text(img: np.ndarray, s: str, org: tuple[int, int], scale: float,
          color: tuple[int, int, int], thick: int = 1) -> None:
    cv2.putText(img, s, org, cv2.FONT_HERSHEY_SIMPLEX, scale, color, thick, cv2.LINE_AA)
