"""Video capture: file / RTSP / webcam in a background thread, latest-frame-only.

* Files loop forever and are paced at their native FPS (so a demo video plays in real time).
* RTSP streams reconnect automatically with exponential backoff (1s → 30s).
* Only the newest frame is kept, so processing never lags behind the camera.
* Frames are resized to ``max_width`` (default 960 px) before anyone sees them.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Prefer TCP for RTSP: far fewer smeared/grey frames than UDP on busy Wi-Fi/NVR links.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


@dataclass
class SourceInfo:
    kind: str  # file | rtsp | webcam
    target: str | int
    label: str


def parse_source(source: str | int, root: Path | None = None) -> SourceInfo:
    """Classify a ``source`` config value. Credentials in RTSP URLs are masked in the label."""
    if isinstance(source, int) or (isinstance(source, str) and source.strip().isdigit()):
        idx = int(source)
        return SourceInfo("webcam", idx, f"Webcam {idx}")
    s = str(source).strip()
    if s.lower().startswith(("rtsp://", "rtsps://", "http://", "https://")):
        label = s
        if "@" in s:
            scheme, rest = s.split("://", 1)
            label = f"{scheme}://***@{rest.split('@', 1)[1]}"
        return SourceInfo("rtsp", s, label)
    path = Path(s)
    if not path.is_absolute() and root is not None:
        path = root / path
    return SourceInfo("file", str(path), path.name)


def resize_max_width(frame: np.ndarray, max_width: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w <= max_width:
        return frame
    scale = max_width / w
    return cv2.resize(frame, (max_width, int(round(h * scale))), interpolation=cv2.INTER_AREA)


class VideoSource:
    """Background capture thread exposing the most recent frame."""

    def __init__(self, info: SourceInfo, max_width: int = 960) -> None:
        self.info = info
        self.max_width = max_width
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._frame_id = 0
        self._frame_time = 0.0
        self._running = False
        self._thread: threading.Thread | None = None
        self.status = "starting"
        self.error: str | None = None
        self.fps = 0.0

    # --------------------------------------------------------------- public
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="video-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)

    def latest(self) -> tuple[int, Optional[np.ndarray], float]:
        """``(frame_id, frame, capture_time)``; frame is shared — copy before drawing on it."""
        with self._lock:
            return self._frame_id, self._frame, self._frame_time

    def is_live(self) -> bool:
        return self.status == "streaming" and time.time() - self._frame_time < 5

    # -------------------------------------------------------------- capture
    def _open(self) -> cv2.VideoCapture:
        if self.info.kind == "webcam":
            backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
            cap = cv2.VideoCapture(int(self.info.target), backend)
        elif self.info.kind == "rtsp":
            cap = cv2.VideoCapture(str(self.info.target), cv2.CAP_FFMPEG)
        else:
            cap = cv2.VideoCapture(str(self.info.target))
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:  # not all backends support it
            pass
        return cap

    def _run(self) -> None:
        backoff = 1.0
        while self._running:
            if self.info.kind == "file" and not Path(str(self.info.target)).exists():
                self.status, self.error = "error", f"Video file not found: {self.info.target}"
                log.error(self.error)
                return
            self.status = "connecting"
            cap = self._open()
            if not cap.isOpened():
                self.status, self.error = "reconnecting", f"Cannot open {self.info.label}"
                log.warning("%s — retrying in %.0fs", self.error, backoff)
                cap.release()
                self._sleep(backoff)
                backoff = min(30.0, backoff * 2)
                continue
            backoff = 1.0
            self.status, self.error = "streaming", None
            log.info("Video source open: %s", self.info.label)
            native_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
            if not 1 <= native_fps <= 120:
                native_fps = 25.0
            self._read_loop(cap, native_fps)
            cap.release()
            if self._running and self.info.kind != "file":
                self.status = "reconnecting"
                log.warning("Lost %s — reconnecting in %.0fs", self.info.label, backoff)
                self._sleep(backoff)
                backoff = min(30.0, backoff * 2)

    def _read_loop(self, cap: cv2.VideoCapture, native_fps: float) -> None:
        period = 1.0 / native_fps
        next_t = time.monotonic()
        fps_count, fps_t0 = 0, time.monotonic()
        failures = 0
        while self._running:
            ok, frame = cap.read()
            if not ok or frame is None:
                if self.info.kind == "file":
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # loop the demo video
                    failures += 1
                    if failures > 3:
                        self.status, self.error = "error", f"Cannot decode {self.info.label}"
                        log.error(self.error)
                        self._running = False
                    continue
                return  # stream dropped → caller reconnects
            failures = 0
            frame = resize_max_width(frame, self.max_width)
            with self._lock:
                self._frame = frame
                self._frame_id += 1
                self._frame_time = time.time()
            fps_count += 1
            now = time.monotonic()
            if now - fps_t0 >= 2.0:
                self.fps = fps_count / (now - fps_t0)
                fps_count, fps_t0 = 0, now
            if self.info.kind == "file":  # pace files at their real speed
                next_t += period
                delay = next_t - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.monotonic()

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(0.2)
