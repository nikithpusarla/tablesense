"""Staff vs guest classification.

Three interchangeable strategies behind one abstract interface:

* :class:`UniformColorClassifier` — STAFF if > ``min_ratio`` of the upper-body crop
  (top 50 % of the box) falls inside the uniform's HSV range (picked on the setup page).
* :class:`PostureMotionClassifier` — seated & stationary inside a table zone for
  ``seated_guest_sec`` → GUEST; standing/moving briefly at a table → likely STAFF.
* :class:`CombinedClassifier` — uniform evidence wins; otherwise posture decides.

Per-track votes are smoothed over time, so one odd frame never flips a person's role.
A trained model can be dropped in later by subclassing :class:`StaffClassifier`
(see :class:`TrainedModelClassifier`).
"""
from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

import cv2
import numpy as np

from .detector import Person

log = logging.getLogger(__name__)


@dataclass
class TrackMemory:
    """What we remember about one ByteTrack ID."""

    votes: deque = field(default_factory=lambda: deque(maxlen=15))  # recent staff scores 0..1
    feet: deque = field(default_factory=lambda: deque(maxlen=60))   # (t, x, y, box_height)
    seated_since: Optional[float] = None
    confirmed_guest: bool = False
    last_seen: float = 0.0


class StaffClassifier(ABC):
    """Interface: set ``person.is_staff`` / ``person.staff_score`` / ``person.seated``."""

    def __init__(self) -> None:
        self.tracks: dict[int, TrackMemory] = {}

    def memory(self, person: Person, now: float) -> TrackMemory:
        mem = self.tracks.setdefault(person.track_id, TrackMemory())
        mem.last_seen = now
        mem.feet.append((now, *person.foot, person.height))
        return mem

    def forget_stale(self, now: float, ttl: float = 30.0) -> None:
        for tid in [t for t, m in self.tracks.items() if now - m.last_seen > ttl]:
            del self.tracks[tid]

    def classify_all(self, frame: np.ndarray, people: list[Person], now: float) -> None:
        for p in people:
            score = self.score(frame, p, now)
            if p.track_id >= 0:
                mem = self.memory(p, now) if p.track_id not in self.tracks else self.tracks[p.track_id]
                mem.votes.append(score)
                score = float(np.mean(mem.votes))
            p.staff_score = score
            p.is_staff = score >= 0.5
        self.forget_stale(now)

    @abstractmethod
    def score(self, frame: np.ndarray, person: Person, now: float) -> float:
        """Return a staff likelihood in [0, 1] for this frame."""


# --------------------------------------------------------------------------- uniform
class UniformColorClassifier(StaffClassifier):
    """Uniform colour match on the upper body (top 50 % of the box)."""

    def __init__(self, lower: list[int] | None, upper: list[int] | None, min_ratio: float = 0.30) -> None:
        super().__init__()
        self.lower = np.array(lower, np.uint8) if lower else None
        self.upper = np.array(upper, np.uint8) if upper else None
        self.min_ratio = min_ratio

    @property
    def configured(self) -> bool:
        return self.lower is not None and self.upper is not None

    def ratio(self, frame: np.ndarray, person: Person) -> float:
        """Fraction of upper-body pixels within the uniform HSV range (hue wrap supported)."""
        if not self.configured:
            return 0.0
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = person.box
        # Trim 15 % at each side and skip the head (top 12 %) to focus on the shirt.
        bw, bh = x2 - x1, y2 - y1
        cx1, cx2 = int(max(0, x1 + 0.15 * bw)), int(min(w, x2 - 0.15 * bw))
        cy1, cy2 = int(max(0, y1 + 0.12 * bh)), int(min(h, y1 + 0.5 * bh))
        if cx2 - cx1 < 3 or cy2 - cy1 < 3:
            return 0.0
        hsv = cv2.cvtColor(frame[cy1:cy2, cx1:cx2], cv2.COLOR_BGR2HSV)
        lo, hi = self.lower, self.upper
        if lo[0] <= hi[0]:
            mask = cv2.inRange(hsv, lo, hi)
        else:  # hue wraps around 180 (reds)
            mask = cv2.inRange(hsv, lo, np.array([179, hi[1], hi[2]], np.uint8)) | \
                   cv2.inRange(hsv, np.array([0, lo[1], lo[2]], np.uint8), hi)
        return float(np.count_nonzero(mask)) / mask.size

    def score(self, frame: np.ndarray, person: Person, now: float) -> float:
        r = self.ratio(frame, person)
        person.extra["uniform_ratio"] = round(r, 3)
        # Soft score: 0.5 exactly at the threshold, saturating quickly either side.
        return float(1 / (1 + math.exp(-(r - self.min_ratio) * 25)))


# --------------------------------------------------------------------------- posture
class PostureMotionClassifier(StaffClassifier):
    """Still inside a table zone → guest; standing + moving → staff.

    Motion is measured in *body heights* (net displacement between the first and second
    half of the window, using medians), so eating/gesturing and detector box jitter don't
    count as walking, and the threshold works at any distance from the camera.
    """

    def __init__(self, seated_max_aspect: float = 1.6, stationary_rel: float = 0.35,
                 seated_guest_sec: float = 20.0, motion_window_sec: float = 3.0) -> None:
        super().__init__()
        self.seated_max_aspect = seated_max_aspect
        self.stationary_rel = stationary_rel
        self.seated_guest_sec = seated_guest_sec
        self.motion_window = motion_window_sec

    def posture(self, person: Person, now: float) -> tuple[bool, float]:
        """``(seated, recent_motion_in_body_heights)`` for this person."""
        aspect = person.height / max(1.0, person.width)
        seated = aspect <= self.seated_max_aspect
        motion = 0.0
        mem = self.tracks.get(person.track_id)
        if mem:
            pts = [(x, y, h) for t, x, y, h in mem.feet if now - t <= self.motion_window]
            if len(pts) >= 4:
                half = len(pts) // 2
                med = lambda vals: sorted(vals)[len(vals) // 2]  # noqa: E731
                a, b = pts[:half], pts[half:]
                disp = math.hypot(med([p[0] for p in b]) - med([p[0] for p in a]),
                                  med([p[1] for p in b]) - med([p[1] for p in a]))
                motion = disp / max(10.0, med([p[2] for p in pts]))
        return seated, motion

    def score(self, frame: np.ndarray, person: Person, now: float) -> float:
        mem = self.memory(person, now) if person.track_id >= 0 else TrackMemory()
        seated, motion = self.posture(person, now)
        # A box cut off by the frame edge has an unreliable shape (very common with
        # ceiling cameras), so never read it as "standing".
        truncated = person.box[3] >= frame.shape[0] - 3 or person.box[1] <= 2
        seated = seated or truncated
        stationary = motion < self.stationary_rel
        person.seated = seated
        person.extra["motion"] = round(motion, 2)
        in_zone = person.zone_id is not None
        # Staying still inside a table zone is the strongest guest signal. From steep
        # CCTV angles seated diners can look tall, so posture only speeds this up.
        if in_zone and stationary:
            mem.seated_since = mem.seated_since or now
            needed = self.seated_guest_sec * (0.5 if seated else 1.0)
            if now - mem.seated_since >= needed:
                mem.confirmed_guest = True
        else:
            mem.seated_since = None
        if mem.confirmed_guest:
            return 0.0  # a confirmed diner stays a guest even when they stand up briefly
        if in_zone:
            if stationary:
                # Standing still at a table is ambiguous (waiter taking an order, or a guest
                # about to sit); the per-track vote history from how they arrived decides.
                return 0.15 if seated else 0.45
            return 0.6  # moving at a table: waiter visiting, or a guest arriving
        if seated:
            return 0.3  # e.g. a customer on a bar stool
        return 0.8 if not stationary else 0.55  # standing & moving → staff-like


# --------------------------------------------------------------------------- combined
class CombinedClassifier(StaffClassifier):
    """Uniform colour is strong evidence; posture/motion breaks ties when there is none."""

    def __init__(self, uniform: UniformColorClassifier, posture: PostureMotionClassifier) -> None:
        super().__init__()
        self.uniform = uniform
        self.posture = posture

    def forget_stale(self, now: float, ttl: float = 30.0) -> None:
        super().forget_stale(now, ttl)
        self.posture.forget_stale(now, ttl)

    def score(self, frame: np.ndarray, person: Person, now: float) -> float:
        p_score = self.posture.score(frame, person, now)
        if not self.uniform.configured:
            return p_score
        u_score = self.uniform.score(frame, person, now)
        mem = self.posture.tracks.get(person.track_id)
        if mem and mem.confirmed_guest and u_score < 0.9:
            return 0.0
        return 0.7 * u_score + 0.3 * p_score


# --------------------------------------------------------------------------- plug-in
class TrainedModelClassifier(StaffClassifier):
    """Placeholder for a trained staff/guest model (e.g. a small CNN on person crops).

    Implement :meth:`score` to return P(staff) for the crop ``frame[y1:y2, x1:x2]`` and
    select it with ``staff.method: trained`` + ``staff.model_path`` in config.yaml.
    """

    def __init__(self, model_path: str) -> None:
        super().__init__()
        self.model_path = model_path
        log.warning("TrainedModelClassifier is a stub — falling back to 0.5 scores (%s)", model_path)

    def score(self, frame: np.ndarray, person: Person, now: float) -> float:
        return 0.5


def build_classifier(cfg: dict[str, Any]) -> StaffClassifier:
    """Factory from the ``staff`` config section."""
    st = cfg.get("staff") or {}
    method = str(st.get("method", "combined")).lower()
    hsv = st.get("uniform_hsv") or {}
    uniform = UniformColorClassifier(hsv.get("lower"), hsv.get("upper"),
                                     float(st.get("uniform_min_ratio", 0.30)))
    pc = st.get("posture") or {}
    posture = PostureMotionClassifier(
        seated_max_aspect=float(pc.get("seated_max_aspect", 1.6)),
        stationary_rel=float(pc.get("stationary_rel", 0.35)),
        seated_guest_sec=float(st.get("seated_guest_sec", 20)),
    )
    if method == "uniform":
        if not uniform.configured:
            log.warning("staff.method=uniform but no uniform colour set — use the Setup page")
        return uniform
    if method == "posture":
        return posture
    if method == "trained":
        return TrainedModelClassifier(str(st.get("model_path", "models/staff.onnx")))
    return CombinedClassifier(uniform, posture)
