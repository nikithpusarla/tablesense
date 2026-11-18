"""Person detection + tracking: YOLO11n (persons only) with ByteTrack IDs, CPU-friendly.

Only class 0 (person) is detected. ByteTrack (built into ultralytics) keeps a stable
``track_id`` per person across frames so nobody is counted twice. The *bottom-centre*
of each box (where a person sits/stands) is used for zone checks.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Person:
    track_id: int  # -1 until ByteTrack confirms the track
    box: tuple[float, float, float, float]  # x1, y1, x2, y2 (processing-frame pixels)
    conf: float
    is_staff: bool = False
    staff_score: float = 0.0
    seated: bool = False
    zone_id: Optional[str] = None
    near_zone_id: Optional[str] = None
    anchor_y: float = 1.0  # where along the box height the "position" point sits
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def foot(self) -> tuple[float, float]:
        """Position point used for zone checks: bottom-centre by default (``anchor_y`` = 1.0).

        With steep ceiling cameras a seated person's box reaches past their chair, so a
        higher point (``live.anchor_y`` ≈ 0.7) lands inside the right table more often.
        """
        x1, y1, x2, y2 = self.box
        return (x1 + x2) / 2, y1 + self.anchor_y * (y2 - y1)

    @property
    def width(self) -> float:
        return self.box[2] - self.box[0]

    @property
    def height(self) -> float:
        return self.box[3] - self.box[1]


class DetectorUnavailable(RuntimeError):
    """Raised when ultralytics/torch is not installed or the model can't be loaded."""


class Detector:
    """Thin wrapper around ``ultralytics.YOLO.track`` restricted to persons."""

    def __init__(self, model_path: str = "yolo11n.pt", conf: float = 0.35,
                 imgsz: int = 640, tracker: str = "bytetrack.yaml", threads: int = 0) -> None:
        try:
            from ultralytics import YOLO  # heavy import, done lazily
        except ImportError as exc:  # pragma: no cover - depends on install
            raise DetectorUnavailable(
                "ultralytics is not installed. Run: .venv\\Scripts\\pip install ultralytics") from exc
        try:
            self.model = YOLO(model_path)
        except Exception as exc:  # pragma: no cover
            raise DetectorUnavailable(f"Could not load model {model_path}: {exc}") from exc
        # ultralytics resets torch's thread count on import, so cap it *after* loading.
        # Too many threads on hybrid (P/E-core) laptop CPUs makes inference slower and
        # starves the capture/streaming threads in the same process.
        try:
            import os

            import torch

            n = threads if threads > 0 else max(1, min(4, (os.cpu_count() or 2) // 2))
            # ultralytics' select_device() re-applies its own NUM_THREADS on every predictor
            # setup, so override that constant too or our cap is silently undone.
            import ultralytics.utils as uu
            import ultralytics.utils.torch_utils as tu

            uu.NUM_THREADS = tu.NUM_THREADS = n
            torch.set_num_threads(n)
            log.info("PyTorch CPU threads: %d", n)
        except Exception:  # pragma: no cover
            log.debug("Could not set torch threads", exc_info=True)
        self.conf = conf
        self.imgsz = imgsz
        self.tracker = tracker
        self.last_ms = 0.0
        log.info("YOLO model loaded: %s (conf=%.2f, imgsz=%d)", model_path, conf, imgsz)

    def detect(self, frame: np.ndarray) -> list[Person]:
        """Run detection + tracking on one frame (call on consecutive processed frames)."""
        t0 = time.perf_counter()
        results = self.model.track(frame, persist=True, tracker=self.tracker, classes=[0],
                                   conf=self.conf, imgsz=self.imgsz, device="cpu", verbose=False)
        self.last_ms = (time.perf_counter() - t0) * 1000
        people: list[Person] = []
        if not results:
            return people
        boxes = results[0].boxes
        if boxes is None or len(boxes) == 0:
            return people
        xyxy = boxes.xyxy.cpu().numpy()
        confs = boxes.conf.cpu().numpy()
        ids = boxes.id.cpu().numpy().astype(int) if boxes.id is not None else [-1] * len(xyxy)
        for (x1, y1, x2, y2), c, tid in zip(xyxy, confs, ids, strict=True):
            people.append(Person(track_id=int(tid), box=(float(x1), float(y1), float(x2), float(y2)),
                                 conf=float(c)))
        return people
