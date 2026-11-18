"""Live mode glue: video → YOLO/ByteTrack → zone assignment → staff classifier → engine.

Also renders the annotated MJPEG frame (zones coloured by state with timers, guests in
blue, staff in purple, optional face blur). Nothing is written to disk — frames live in
memory only; only table events reach SQLite (via the engine).
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import cv2
import numpy as np

from .detector import Detector, Person
from .staff_classifier import StaffClassifier
from .table_engine import Observation, TableEngine
from .video_source import VideoSource

log = logging.getLogger(__name__)

STATE_BGR: dict[str, tuple[int, int, int]] = {
    "FREE": (94, 197, 34),
    "SEATED_WAITING": (68, 68, 239),
    "SERVED": (22, 115, 249),
    "NEEDS_CLEANING": (8, 179, 234),
}
GUEST_BGR = (246, 130, 59)   # blue
STAFF_BGR = (247, 85, 168)   # purple


# ------------------------------------------------------------------------ zones
@dataclass
class Zone:
    id: str
    name: str
    seats: int
    poly: np.ndarray  # (N, 2) float32, frame pixels

    @property
    def contour(self) -> np.ndarray:
        return self.poly.reshape(-1, 1, 2)


def zones_to_pixels(zones: list[dict[str, Any]], width: int, height: int) -> list[Zone]:
    """Convert zones.json entries to pixel polygons. Coordinates ≤ 1 are treated as normalised."""
    out: list[Zone] = []
    for z in zones:
        pts = np.array(z.get("polygon") or [], np.float32)
        if len(pts) < 3:
            continue
        if float(pts.max()) <= 1.0:
            pts = pts * np.array([width, height], np.float32)
        out.append(Zone(str(z["id"]), str(z.get("name") or z["id"]), int(z.get("seats", 4)), pts))
    return out


def box_overlap_ratio(box: tuple[float, float, float, float], zone: Zone, grid: int = 6) -> float:
    """Approximate fraction of the person box lying inside the zone (grid sampling)."""
    x1, y1, x2, y2 = box
    xs = np.linspace(x1, x2, grid)
    ys = np.linspace(y1, y2, grid)
    inside = sum(1 for x in xs for y in ys
                 if cv2.pointPolygonTest(zone.contour, (float(x), float(y)), False) >= 0)
    return inside / (grid * grid)


def assign_zones(people: list[Person], zones: list[Zone], *, overlap_min: float = 0.3,
                 adjacent_px: float = 40.0) -> None:
    """Set ``zone_id`` (person is *at* the table) and ``near_zone_id`` (within ``adjacent_px``)."""
    for p in people:
        best: tuple[float, Optional[str]] = (0.0, None)
        nearest: tuple[float, Optional[str]] = (-1e9, None)
        for z in zones:
            dist = cv2.pointPolygonTest(z.contour, p.foot, True)  # >0 inside, <0 outside (px)
            if dist > nearest[0]:
                nearest = (dist, z.id)
            ratio = box_overlap_ratio(p.box, z)
            score = 1.0 + ratio if dist >= 0 else ratio
            if (dist >= 0 or ratio >= overlap_min) and score > best[0]:
                best = (score, z.id)
        p.zone_id = best[1]
        p.near_zone_id = best[1] or (nearest[1] if nearest[0] >= -adjacent_px else None)


def build_observations(people: list[Person], zones: list[Zone]) -> dict[str, Observation]:
    obs = {z.id: Observation() for z in zones}
    for p in people:
        if p.is_staff:
            if p.near_zone_id in obs:
                obs[p.near_zone_id].staff += 1
        elif p.zone_id in obs:
            obs[p.zone_id].guests += 1
    return obs


# --------------------------------------------------------------------- pipeline
class LivePipeline:
    """Background thread running detection on every Nth captured frame."""

    def __init__(self, source: VideoSource, detector: Detector, classifier: StaffClassifier,
                 zones: list[dict[str, Any]], cfg: dict[str, Any]) -> None:
        self.source = source
        self.detector = detector
        self.classifier = classifier
        self.zone_defs = zones
        det = cfg.get("detector") or {}
        live = cfg.get("live") or {}
        self.every_n = max(1, int(det.get("process_every_n", 3)))
        self.overlap_min = float(live.get("zone_overlap_min", 0.3))
        self.adjacent_px = float(live.get("staff_adjacent_px", 40))
        self.anchor_y = min(1.0, max(0.3, float(live.get("anchor_y", 1.0))))
        self.face_blur = bool((cfg.get("privacy") or {}).get("face_blur", False))
        self.engine: TableEngine | None = None
        self._zones: list[Zone] = []
        self._zones_size: tuple[int, int] = (0, 0)
        self._people: list[Person] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self.det_fps = 0.0
        self.error: str | None = None

    def attach(self, engine: TableEngine) -> None:
        self.engine = engine

    def set_zones(self, zones: list[dict[str, Any]]) -> None:
        with self._lock:
            self.zone_defs = zones
            self._zones_size = (0, 0)  # force pixel recompute

    def start(self) -> None:
        self.source.start()
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="live-pipeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        self.source.stop()

    def _zones_for(self, frame: np.ndarray) -> list[Zone]:
        h, w = frame.shape[:2]
        with self._lock:
            if self._zones_size != (w, h):
                self._zones = zones_to_pixels(self.zone_defs, w, h)
                self._zones_size = (w, h)
            return self._zones

    def _loop(self) -> None:
        last_processed = 0
        count, t0 = 0, time.monotonic()
        while self._running:
            fid, frame, _ = self.source.latest()
            if frame is None or fid < last_processed + self.every_n:
                time.sleep(0.005)
                continue
            last_processed = fid
            try:
                self.process(frame, time.time())
                self.error = None
            except Exception as exc:
                self.error = f"Detection error: {exc}"
                log.exception("Live pipeline step failed")
                time.sleep(1)
            count += 1
            if time.monotonic() - t0 >= 2:
                self.det_fps = count / (time.monotonic() - t0)
                count, t0 = 0, time.monotonic()

    def process(self, frame: np.ndarray, now: float) -> list[Person]:
        """One detection step (public so tests/tools can drive it with fake frames)."""
        zones = self._zones_for(frame)
        people = self.detector.detect(frame)
        for p in people:
            p.anchor_y = self.anchor_y
        assign_zones(people, zones, overlap_min=self.overlap_min, adjacent_px=self.adjacent_px)
        self.classifier.classify_all(frame, people, now)
        if self.engine is not None:
            self.engine.update(now, build_observations(people, zones))
        with self._lock:
            self._people = people
        return people

    # ------------------------------------------------------------------ views
    def public_state(self) -> dict[str, Any]:
        with self._lock:
            people = list(self._people)
        return {
            "source": self.source.info.label,
            "source_kind": self.source.info.kind,
            "status": self.source.status,
            "error": self.source.error or self.error,
            "camera_fps": round(self.source.fps, 1),
            "ai_fps": round(self.det_fps, 1),
            "inference_ms": round(self.detector.last_ms),
            "people": len(people),
            "staff": sum(1 for p in people if p.is_staff),
            "zones": len(self.zone_defs),
        }

    def raw_frame(self) -> Optional[np.ndarray]:
        return self.source.latest()[1]

    def render_frame(self, snapshot: dict[str, Any]) -> np.ndarray:
        """Latest camera frame with zones, timers, people and HUD drawn on top."""
        _, frame, _ = self.source.latest()
        if frame is None:
            return self._placeholder()
        img = frame.copy()
        zones = self._zones_for(img)
        with self._lock:
            people = list(self._people)

        if self.face_blur:
            for p in people:
                x1, y1, x2, y2 = (int(v) for v in p.box)
                hy2 = int(y1 + 0.2 * (y2 - y1))
                x1, y1 = max(0, x1), max(0, y1)
                if x2 - x1 > 4 and hy2 - y1 > 4:
                    roi = img[y1:hy2, x1:x2]
                    k = max(3, ((x2 - x1) // 3) | 1)
                    img[y1:hy2, x1:x2] = cv2.GaussianBlur(roi, (k, k), 0)

        by_id = {t["id"]: t for t in snapshot.get("tables", [])}
        overlay = img.copy()
        for z in zones:
            state = by_id.get(z.id, {}).get("state", "FREE")
            cv2.fillPoly(overlay, [z.poly.astype(np.int32)], STATE_BGR.get(state, (128, 128, 128)))
        cv2.addWeighted(overlay, 0.22, img, 0.78, 0, img)
        for z in zones:
            t = by_id.get(z.id, {})
            color = STATE_BGR.get(t.get("state", "FREE"), (128, 128, 128))
            cv2.polylines(img, [z.poly.astype(np.int32)], True, color, 2, cv2.LINE_AA)
            el = int(t.get("elapsed_sec", 0))
            label = f"{z.name}  {el // 60:02d}:{el % 60:02d}"
            if t.get("guest_count"):
                label += f"  {t['guest_count']}p"
            top = z.poly[np.argmin(z.poly[:, 1])]
            _label(img, label, (int(top[0]), int(top[1]) - 6), color)

        for p in people:
            x1, y1, x2, y2 = (int(v) for v in p.box)
            color = STAFF_BGR if p.is_staff else GUEST_BGR
            cv2.rectangle(img, (x1, y1), (x2, y2), color, 2, cv2.LINE_AA)
            fx, fy = (int(v) for v in p.foot)
            cv2.circle(img, (fx, fy), 4, color, -1, cv2.LINE_AA)
            tag = f"{'Staff' if p.is_staff else 'Guest'}{'' if p.track_id < 0 else f' #{p.track_id}'}"
            _label(img, tag, (x1, y1 - 4), color, scale=0.42)

        self._hud(img, people)
        return img

    def _hud(self, img: np.ndarray, people: list[Person]) -> None:
        w = img.shape[1]
        bar = img.copy()
        cv2.rectangle(bar, (0, 0), (w, 30), (15, 10, 8), -1)
        cv2.addWeighted(bar, 0.65, img, 0.35, 0, img)
        cv2.circle(img, (14, 15), 5, (60, 60, 240), -1, cv2.LINE_AA)
        staff = sum(1 for p in people if p.is_staff)
        text = (f"LIVE  {datetime.now().strftime('%H:%M:%S')}   cam {self.source.fps:.0f} fps"
                f"  AI {self.det_fps:.1f} fps ({self.detector.last_ms:.0f} ms)"
                f"   {len(people)} people ({staff} staff)")
        cv2.putText(img, text, (26, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (235, 235, 235), 1, cv2.LINE_AA)
        if not self.zone_defs:
            msg = "No table zones yet - open Setup to draw them"
            cv2.putText(img, msg, (12, img.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 215, 255), 2, cv2.LINE_AA)

    def _placeholder(self) -> np.ndarray:
        img = np.full((540, 960, 3), (32, 23, 15), np.uint8)
        status = self.source.error or f"Connecting to {self.source.info.label} ..."
        cv2.putText(img, "CAMERA", (40, 240), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (8, 179, 234), 2, cv2.LINE_AA)
        cv2.putText(img, status[:80], (40, 290), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (226, 232, 240), 1, cv2.LINE_AA)
        return img


def _label(img: np.ndarray, text: str, org: tuple[int, int], color: tuple[int, int, int],
           scale: float = 0.5) -> None:
    """Filled label with white text, kept inside the frame."""
    (tw, th), base = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    x = max(0, min(img.shape[1] - tw - 6, org[0]))
    y = max(th + 36, org[1])  # stay below the 30px HUD bar
    cv2.rectangle(img, (x, y - th - 5), (x + tw + 6, y + base - 1), color, -1)
    cv2.putText(img, text, (x + 3, y - 2), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 1, cv2.LINE_AA)
