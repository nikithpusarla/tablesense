"""TableSense FastAPI application: routes, WebSocket push, MJPEG feed, startup wiring.

Run from the project root::

    python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import os
import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import cv2
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics
from .config import DB_PATH, ROOT, STATIC_DIR, load_config, load_zones, save_config_values, save_zones
from .db import Database
from .notifier import Notifier
from .perf import disable_windows_power_throttling, limit_opencv_threads
from .queue_predictor import HistoryStats, QueueManager, predict
from .simulator import Simulator
from .table_engine import EngineSettings, TableEngine

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("tablesense")
load_dotenv(ROOT / ".env")


class AppState:
    """Holds the running components; built once at startup."""

    def __init__(self) -> None:
        self.cfg = load_config()
        disable_windows_power_throttling()
        self.started = time.time()
        self.db = Database(DB_PATH)
        self.notifier = Notifier(self.cfg)
        self.queue = QueueManager()
        self.stats = HistoryStats(
            self.db,
            fallback_session_min=float(self.cfg.get_path("analytics.fallback_session_min", 50)),
            fallback_cleaning_min=float(self.cfg.get_path("queue.fallback_cleaning_min", 3)),
        )
        self.mode = str(self.cfg.get("mode", "simulation")).lower()
        self.simulator: Simulator | None = None
        self.live: Any = None  # LivePipeline when mode == live
        self.notice: str | None = None  # shown as a banner on the dashboard
        self.engine: TableEngine
        self._frame_lock = threading.Lock()
        self._frame_cache: tuple[float, bytes] = (0.0, b"")
        settings = EngineSettings.from_config(self.cfg)

        if self.mode == "live" and not self._build_live(settings):
            self.mode = "simulation"
        if self.mode != "live":
            self.mode = "simulation"
            self.simulator = Simulator(self.cfg, queue=self.queue)
            self.engine = TableEngine(self.simulator.layout(), settings, db=self.db,
                                      source="sim", on_alert=self.notifier,
                                      start_time=self.simulator.now())
            self.simulator.attach(self.engine)

        if self.cfg.get_path("analytics.seed_demo_data", False) and self.engine.tables:
            analytics.seed_demo_history(self.db, self.table_defs(), self.cfg,
                                        weeks=int(self.cfg.get_path("analytics.seed_weeks", 4)))

    def _build_live(self, settings: EngineSettings) -> bool:
        """Wire camera -> YOLO/ByteTrack -> classifier -> engine. False (with notice) on failure."""
        from .config import load_zones
        from .detector import Detector, DetectorUnavailable
        from .live_pipeline import LivePipeline
        from .staff_classifier import build_classifier
        from .video_source import VideoSource, parse_source

        info = parse_source(self.cfg.get("source", 0), ROOT)
        if info.kind == "file" and not Path(str(info.target)).exists():
            self.notice = (f"Live mode: video file '{self.cfg.get('source')}' was not found. Drop an .mp4 "
                           "into the videos/ folder (or set 'source' in config.yaml). Showing SIMULATION instead.")
            log.error(self.notice)
            return False
        model = str(self.cfg.get_path("detector.model", "yolo11n.pt"))
        model_path = Path(model) if Path(model).is_absolute() else ROOT / "models" / model
        model_path.parent.mkdir(parents=True, exist_ok=True)
        limit_opencv_threads(2)
        try:
            detector = Detector(str(model_path), conf=float(self.cfg.get_path("detector.conf", 0.35)),
                                imgsz=int(self.cfg.get_path("detector.imgsz", 640)),
                                threads=int(self.cfg.get_path("detector.threads", 0)))
        except DetectorUnavailable as exc:
            self.notice = f"Live mode unavailable ({exc}). Showing SIMULATION instead."
            log.error(self.notice)
            return False
        zones = load_zones()
        tables = [{"id": z["id"], "name": z.get("name", z["id"]), "seats": z.get("seats", 4),
                   "polygon": z.get("polygon", [])} for z in zones]
        self.engine = TableEngine(tables, settings, db=self.db, source="live",
                                  on_alert=self.notifier, start_time=time.time())
        source = VideoSource(info, max_width=int(self.cfg.get_path("detector.max_width", 960)))
        self.live = LivePipeline(source, detector, build_classifier(self.cfg), zones, self.cfg)
        self.live.attach(self.engine)
        if not zones:
            self.notice = "No table zones yet - open Setup to draw them on the camera image."
        log.info("Live mode: %s, %d table zones", info.label, len(zones))
        return True

    def apply_zones(self, payload: dict[str, Any]) -> int:
        """Persist zones.json and hot-reload them into the live engine/pipeline."""
        save_zones(payload)
        tables = payload.get("tables", [])
        if self.live:
            self.engine.set_tables(tables, self.now())
            self.live.set_zones(tables)
            self.notice = None if tables else "No table zones yet - open Setup to draw them on the camera image."
        return len(tables)

    def apply_staff(self, updates: dict[str, Any]) -> None:
        """Save staff-classifier settings to config.yaml and rebuild the classifier."""
        save_config_values({f"staff.{k}": v for k, v in updates.items()})
        staff = dict(self.cfg.get("staff") or {})
        staff.update(updates)
        self.cfg["staff"] = staff
        if self.live:
            from .staff_classifier import build_classifier
            self.live.classifier = build_classifier(self.cfg)

    def table_defs(self) -> list[dict[str, Any]]:
        """Current table list (id, name, seats) for analytics/seeding."""
        return [{"id": t.id, "name": t.name, "seats": t.seats} for t in self.engine.tables.values()]

    # ---------------------------------------------------------------- clock
    def now(self) -> float:
        return self.simulator.now() if self.simulator else time.time()

    def start(self) -> None:
        if self.simulator:
            self.simulator.warmup()
            self.simulator.start()
        if self.live:
            self.live.start()

    def stop(self) -> None:
        if self.simulator:
            self.simulator.stop()
        if self.live:
            self.live.stop()
        self.db.close()

    # ---------------------------------------------------------------- state
    def full_state(self) -> dict[str, Any]:
        now = self.now()
        snap = self.engine.snapshot(now)
        snap.pop("recent_events", None)
        predictions, queue_view = predict(snap["tables"], self.queue.entries(), now, self.stats)
        snap.update({
            "mode": self.mode,
            "restaurant": self.cfg.get_path("restaurant.name", "TableSense"),
            "server_time": time.time(),
            "notify_seq": self.notifier.last_seq,
            "sim": self.simulator.public_state() if self.simulator else None,
            "live": self.live.public_state() if self.live else None,
            "notice": self.notice or self._perf_notice(),
            "queue": queue_view,
            "predictions": predictions,
            "queue_history": list(self.queue.history)[-15:],
            "thresholds": {
                "waiter_alert_sec": self.engine.settings.waiter_alert_sec,
                "clean_alert_sec": self.engine.settings.clean_alert_sec,
                "escalate_sec": self.engine.settings.escalate_sec,
            },
        })
        return snap

    def _perf_notice(self) -> str | None:
        """Warn on the dashboard when the AI is too slow to track tables reliably."""
        if not self.live or time.time() - self.started < 45:
            return None
        ls = self.live.public_state()
        if ls["status"] == "streaming" and 0 < ls["ai_fps"] < 1.5:
            return (f"AI is running slowly ({ls['ai_fps']} frames/s, {ls['inference_ms']} ms each). "
                    "Plug the laptop into power, close heavy apps, or set detector.imgsz: 480 in config.yaml.")
        return None

    def jpeg_frame(self) -> bytes:
        """Latest annotated frame as JPEG; cached ~80 ms so many viewers share one render."""
        with self._frame_lock:
            ts, data = self._frame_cache
            if time.monotonic() - ts < 0.08 and data:
                return data
            snap = self.engine.snapshot(self.now())
            frame = self.live.render_frame(snap) if self.live else self.simulator.render_frame(snap)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            data = buf.tobytes() if ok else data
            self._frame_cache = (time.monotonic(), data)
            return data

    def raw_jpeg(self) -> bytes | None:
        """Un-annotated current camera frame (for the setup page); floor plan in simulation."""
        if self.live:
            frame = self.live.raw_frame()
            if frame is None:
                return None
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            return buf.tobytes() if ok else None
        return self.jpeg_frame()


state: AppState | None = None


def S() -> AppState:
    if state is None:
        raise HTTPException(503, "Starting up")
    return state


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global state
    state = AppState()
    state.start()
    port = os.getenv("PORT") or state.cfg.get_path("server.port", 8000)
    log.info("TableSense ready - mode=%s - open http://localhost:%s", state.mode, port)
    yield
    state.stop()


app = FastAPI(title="TableSense", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _page(name: str) -> FileResponse:
    path = STATIC_DIR / name
    if not path.exists():
        raise HTTPException(404, f"{name} not built yet")
    return FileResponse(path)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return _page("index.html")


@app.get("/setup", include_in_schema=False)
def setup_page() -> FileResponse:
    return _page("setup.html")


@app.get("/analytics", include_in_schema=False)
def analytics_page() -> FileResponse:
    return _page("analytics.html")


# ------------------------------------------------------------------ state API
@app.get("/api/state")
def api_state() -> JSONResponse:
    return JSONResponse(S().full_state())


@app.post("/api/alerts/{alert_id}/ack")
def ack_alert(alert_id: int) -> dict[str, Any]:
    if not S().engine.ack_alert(alert_id, S().now()):
        raise HTTPException(404, "Alert not found or already resolved")
    return {"ok": True}


@app.post("/api/tables/{table_id}/mark_clean")
def mark_clean(table_id: str) -> dict[str, Any]:
    st = S()
    if not st.engine.mark_clean(table_id, st.now()):
        raise HTTPException(404, "Unknown table")
    if st.simulator:
        st.simulator.on_manual_clean(table_id)
    return {"ok": True}


class SpeedIn(BaseModel):
    speed: float = Field(gt=0, le=120)


@app.post("/api/sim/speed")
def sim_speed(body: SpeedIn) -> dict[str, Any]:
    st = S()
    if not st.simulator:
        raise HTTPException(400, "Not in simulation mode")
    return {"ok": True, "speed": st.simulator.set_speed(body.speed)}


class QueueIn(BaseModel):
    name: str = Field(default="Guest", max_length=40)
    size: int = Field(ge=1, le=20)
    note: str = Field(default="", max_length=80)


@app.get("/api/queue")
def get_queue() -> list[dict[str, Any]]:
    return S().full_state()["queue"]


@app.post("/api/queue")
def add_to_queue(body: QueueIn) -> dict[str, Any]:
    st = S()
    entry = st.queue.add(body.name, body.size, st.now(), source="host", note=body.note)
    return {"ok": True, "id": entry.id}


@app.delete("/api/queue/{entry_id}")
def remove_from_queue(entry_id: int, reason: str = "removed",
                      table_id: str | None = None) -> dict[str, Any]:
    st = S()
    if reason not in ("seated", "removed", "walked_away"):
        raise HTTPException(400, "reason must be seated, removed or walked_away")
    if st.queue.remove(entry_id, st.now(), reason=reason, table_id=table_id) is None:
        raise HTTPException(404, "Queue entry not found")
    return {"ok": True}


@app.get("/api/analytics/weekly")
def analytics_weekly(weeks: int = 1) -> JSONResponse:
    st = S()
    return JSONResponse(analytics.weekly_report(st.db, st.table_defs(), st.cfg, weeks=weeks))


@app.get("/api/analytics/export.csv")
def analytics_export(weeks: int = 1, kind: str = "daily") -> Response:
    st = S()
    if kind not in ("daily", "events"):
        raise HTTPException(400, "kind must be daily or events")
    text = analytics.export_csv(st.db, st.table_defs(), st.cfg, weeks=weeks, kind=kind)
    fname = f"tablesense_{kind}_{weeks}w_{time.strftime('%Y%m%d')}.csv"
    return Response(text, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


class ZoneIn(BaseModel):
    id: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=1, max_length=30)
    seats: int = Field(ge=1, le=30)
    polygon: list[list[float]] = Field(min_length=3, max_length=40)


class ZonesIn(BaseModel):
    tables: list[ZoneIn] = Field(max_length=100)
    frame_size: list[int] | None = None


@app.get("/api/zones")
def get_zones() -> dict[str, Any]:
    st = S()
    return {
        "tables": load_zones(),
        "mode": st.mode,
        "uniform_hsv": st.cfg.get_path("staff.uniform_hsv"),
        "staff_method": st.cfg.get_path("staff.method", "combined"),
        "uniform_min_ratio": st.cfg.get_path("staff.uniform_min_ratio", 0.3),
    }


@app.post("/api/zones")
def post_zones(body: ZonesIn) -> dict[str, Any]:
    ids = [t.id for t in body.tables]
    if len(ids) != len(set(ids)):
        raise HTTPException(400, "Table ids must be unique")
    for t in body.tables:
        if any(len(pt) != 2 or not (0 <= pt[0] <= 1 and 0 <= pt[1] <= 1) for pt in t.polygon):
            raise HTTPException(400, f"{t.id}: polygon points must be normalised [x, y] in 0..1")
    payload = {"frame_size": body.frame_size, "tables": [t.model_dump() for t in body.tables]}
    n = S().apply_zones(payload)
    return {"ok": True, "tables": n, "applied_live": S().mode == "live"}


class UniformIn(BaseModel):
    lower: list[int] | None = Field(default=None, min_length=3, max_length=3)
    upper: list[int] | None = Field(default=None, min_length=3, max_length=3)
    method: str | None = None
    min_ratio: float | None = Field(default=None, gt=0, lt=1)


@app.post("/api/uniform")
def post_uniform(body: UniformIn) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if body.lower is not None and body.upper is not None:
        lo = [max(0, min(179 if i == 0 else 255, v)) for i, v in enumerate(body.lower)]
        hi = [max(0, min(179 if i == 0 else 255, v)) for i, v in enumerate(body.upper)]
        updates["uniform_hsv"] = {"lower": lo, "upper": hi}
    elif body.lower is None and body.upper is None and body.method is None and body.min_ratio is None:
        updates["uniform_hsv"] = None  # clear
    if body.method is not None:
        if body.method not in ("uniform", "posture", "combined"):
            raise HTTPException(400, "method must be uniform, posture or combined")
        updates["method"] = body.method
    if body.min_ratio is not None:
        updates["uniform_min_ratio"] = round(body.min_ratio, 2)
    S().apply_staff(updates)
    return {"ok": True, **updates}


@app.get("/api/source_frame")
def source_frame() -> Response:
    """Grab one frame straight from the configured video source (works in simulation mode too)."""
    from .video_source import parse_source, resize_max_width
    st = S()
    info = parse_source(st.cfg.get("source", 0), ROOT)
    if info.kind == "file" and not Path(str(info.target)).exists():
        raise HTTPException(404, f"Video file not found: {st.cfg.get('source')}")
    if st.live and st.live.raw_frame() is not None:
        frame = st.live.raw_frame()
    else:
        target = int(info.target) if info.kind == "webcam" else str(info.target)
        cap = cv2.VideoCapture(target)
        try:
            if info.kind == "file":  # skip the first second (often black)
                cap.set(cv2.CAP_PROP_POS_FRAMES, int((cap.get(cv2.CAP_PROP_FPS) or 25)))
            ok, frame = cap.read()
        finally:
            cap.release()
        if not ok or frame is None:
            raise HTTPException(502, f"Could not read a frame from {info.label}")
        frame = resize_max_width(frame, int(st.cfg.get_path("detector.max_width", 960)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return Response(buf.tobytes(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/api/notifications")
def notifications(since: int = 0) -> list[dict[str, Any]]:
    return S().notifier.since(since)


# ------------------------------------------------------------------ live push
@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    st = S()
    seq = st.notifier.last_seq  # only notify about alerts raised after connecting
    try:
        while True:
            payload = st.full_state()
            payload["notifications"] = st.notifier.since(seq)
            seq = payload["notify_seq"]
            await websocket.send_json(payload)
            await asyncio.sleep(1.0)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("WebSocket error")


@app.get("/video_feed")
async def video_feed() -> StreamingResponse:
    st = S()

    async def gen() -> AsyncIterator[bytes]:
        while True:
            data = await asyncio.to_thread(st.jpeg_frame)
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                   + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
            await asyncio.sleep(0.1)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/snapshot")
def snapshot() -> Response:
    data = S().raw_jpeg()
    if data is None:
        raise HTTPException(503, "No camera frame yet - check the video source")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
