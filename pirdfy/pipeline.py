"""Per-camera worker: capture -> motion gate -> detection -> visits, photos, clips."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .camera import CameraBackend, make_backend
from .config import CameraSettings, ConfigStore
from .db import Database
from .detector import Detection, Detector
from .events import EventBus
from .motion import MotionGate
from .notify import Notifier, VisitNotice
from .storage import MediaStore, encode_jpeg

log = logging.getLogger(__name__)


@dataclass
class Visit:
    id: int
    started_at: float
    last_seen: float
    photos: int = 0
    last_photo_at: float = 0.0
    max_count: int = 0
    cover_confidence: float = 0.0


@dataclass
class Clip:
    path: str
    started_at: float
    visit_id: int | None


@dataclass
class Counters:
    frames: int = 0
    detector_runs: int = 0
    window_start: float = field(default_factory=time.time)
    window_frames: int = 0
    fps: float = 0.0


def crop(rgb: np.ndarray, d: Detection, pad: float = 0.25) -> np.ndarray:
    h, w = rgb.shape[:2]
    px, py = d.w * pad, d.h * pad
    x1, y1 = max(0, int((d.x - px) * w)), max(0, int((d.y - py) * h))
    x2, y2 = min(w, int((d.x + d.w + px) * w)), min(h, int((d.y + d.h + py) * h))
    return rgb[y1:y2, x1:x2]


class CameraWorker:
    def __init__(self, cam: CameraSettings, *, store: ConfigStore, db: Database, media: MediaStore,
                 detector: Detector, notifier: Notifier, bus: EventBus,
                 backend_factory: Callable[[CameraSettings], CameraBackend] = make_backend,
                 clock: Callable[[], float] = time.time):
        self.cam = cam
        self.store = store
        self.db = db
        self.media = media
        self.detector = detector
        self.notifier = notifier
        self.bus = bus
        self.backend_factory = backend_factory
        self.clock = clock

        self.backend: CameraBackend | None = None
        self.gate = MotionGate(store.settings.capture.motion)
        self.visit: Visit | None = None
        self.clip: Clip | None = None
        self.last_clip_end = 0.0
        self.last_detect_at = 0.0
        self.paused = False
        self.error: str | None = None
        self.counters = Counters()
        self.last_detections: list[Detection] = []

        self._latest: np.ndarray | None = None
        self._latest_lock = threading.Lock()
        self._snapshot_requested = threading.Event()
        self._restart = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.cam.id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._thread = None

    def apply_settings(self, cam: CameraSettings) -> None:
        old, self.cam = self.cam, cam
        changed = {k for k in CameraSettings.model_fields if getattr(old, k) != getattr(cam, k)}
        if changed & CameraSettings.RESTART_FIELDS:
            self._restart.set()
        elif changed and self.backend is not None:
            try:
                self.backend.apply_controls(cam)
            except Exception as e:
                log.warning("Camera %d: could not apply controls: %s", cam.id, e)

    def request_snapshot(self) -> None:
        self._snapshot_requested.set()

    def latest_frame(self) -> np.ndarray | None:
        with self._latest_lock:
            return self._latest

    def status(self) -> dict:
        return {
            "id": self.cam.id, "name": self.cam.name, "model": self.backend.model if self.backend else None,
            "running": self.backend is not None and self.error is None, "paused": self.paused,
            "error": self.error, "fps": round(self.counters.fps, 1),
            "motion": round(self.gate.last_score, 4), "frames": self.counters.frames,
            "detector_runs": self.counters.detector_runs,
            "visit": {"id": self.visit.id, "started_at": self.visit.started_at,
                      "count": self.visit.max_count} if self.visit else None,
            "recording": self.clip is not None,
            "detections": [d.__dict__ for d in self.last_detections],
        }

    # -- main loop ----------------------------------------------------------------------
    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._open()
                backoff = 2.0
                while not self._stop.is_set() and not self._restart.is_set():
                    t0 = time.monotonic()
                    self.step()
                    self._stop.wait(max(0.0, self.store.settings.capture.interval - (time.monotonic() - t0)))
            except Exception as e:
                self.error = str(e) or type(e).__name__
                log.error("Camera %d failed: %s (retrying in %.0fs)", self.cam.id, self.error, backoff)
                self.bus.publish("camera", {"id": self.cam.id, "error": self.error})
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                self._close()
                self._restart.clear()

    def _open(self) -> None:
        self.backend = self.backend_factory(self.cam)
        self.backend.open()
        self.gate.reset()
        self.error = None
        self.bus.publish("camera", {"id": self.cam.id, "error": None})

    def _close(self) -> None:
        now = self.clock()
        self._stop_clip(now)
        self._end_visit(now)
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:
                log.exception("Camera %d close failed", self.cam.id)
        self.backend = None

    def step(self) -> None:
        """Process one frame. Public for tests."""
        assert self.backend is not None
        settings = self.store.settings
        cap = settings.capture
        self.gate.settings = cap.motion
        dets: list[Detection] = []
        main: np.ndarray | None = None
        snapshot = self._snapshot_requested.is_set()
        with self.backend.capture() as frame:
            now = frame.ts
            lores = frame.lores_rgb()
            with self._latest_lock:
                self._latest = lores
            self._count_frame(now)
            if not self.paused and self.detector.ready:
                moving = self.gate.triggered(frame.gray())
                idle_due = now - self.last_detect_at >= cap.motion.idle_detect_seconds
                if self.visit is not None or moving or idle_due:
                    self.last_detect_at = now
                    dets = self.detector.detect(lores)
                    self.counters.detector_runs += 1
            if (dets and self._wants_photo(now, len(dets))) or snapshot:
                main = frame.main_rgb()  # only valid inside the capture context

        self.last_detections = dets
        if snapshot and main is not None:
            self._snapshot_requested.clear()
            self._save_manual(main, now)
        if dets:
            self._on_detections(dets, main, now)
        elif self.visit is not None and now - self.visit.last_seen > cap.visit_gap:
            self._end_visit(now)
        self._manage_clip(now, bool(dets))

    # -- visits -----------------------------------------------------------------------
    def _wants_photo(self, now: float, count: int) -> bool:
        cap = self.store.settings.capture
        v = self.visit
        if not self.media.can_write():
            return False
        if v is None or v.photos == 0:
            return True
        if v.photos >= cap.max_photos_per_visit:
            return False
        return count > v.max_count or now - v.last_photo_at >= cap.photo_interval

    def _on_detections(self, dets: list[Detection], main: np.ndarray | None, now: float) -> None:
        best = dets[0]
        new_visit = self.visit is None
        if new_visit:
            vid = self.db.start_visit(self.cam.id, now)
            self.visit = Visit(id=vid, started_at=now, last_seen=now)
            log.info("Camera %d: visit %d started (%d x %s, %.0f%%)", self.cam.id, vid, len(dets),
                     best.label, best.confidence * 100)
        v = self.visit
        assert v is not None
        v.last_seen = now
        self.db.touch_visit(v.id, now, len(dets), best.confidence, {d.label for d in dets})
        if main is not None:
            self._save_photo(main, dets, now)
        v.max_count = max(v.max_count, len(dets))
        if new_visit:
            self.bus.publish("visit_started", {"camera_id": self.cam.id, "visit_id": v.id,
                                               "count": len(dets), "label": best.label})
            image = None
            if main is not None and self.store.settings.notifications.attach_image:
                image = encode_jpeg(crop(main, best), 85, 1024)
            self.notifier.visit_started(VisitNotice(camera=self.cam.name, count=len(dets), label=best.label,
                                                    confidence=best.confidence, ts=now, image=image))
            self._maybe_start_clip(now)

    def _save_photo(self, main: np.ndarray, dets: list[Detection], now: float) -> None:
        v = self.visit
        assert v is not None
        info = self.media.save_photo(main, camera_id=self.cam.id, ts=now,
                                     quality=self.store.settings.capture.jpeg_quality)
        pid = self.db.add_photo(visit_id=v.id, camera_id=self.cam.id, taken_at=now,
                                detections=[d.as_row() for d in dets], **info)
        v.photos += 1
        v.last_photo_at = now
        score = dets[0].confidence + 0.05 * (len(dets) - 1)
        if score > v.cover_confidence:
            v.cover_confidence = score
            self.db.set_cover(v.id, pid)
        self.bus.publish("photo", {"camera_id": self.cam.id, "visit_id": v.id, "photo_id": pid,
                                   "count": len(dets)})

    def _save_manual(self, main: np.ndarray, now: float) -> None:
        vid = self.db.start_visit(self.cam.id, now)
        info = self.media.save_photo(main, camera_id=self.cam.id, ts=now,
                                     quality=self.store.settings.capture.jpeg_quality)
        pid = self.db.add_photo(visit_id=vid, camera_id=self.cam.id, taken_at=now, detections=[], **info)
        self.db.touch_visit(vid, now, 0, 0.0, {"snapshot"})
        self.db.set_cover(vid, pid)
        self.bus.publish("photo", {"camera_id": self.cam.id, "visit_id": vid, "photo_id": pid, "count": 0})

    def _end_visit(self, now: float) -> None:
        v = self.visit
        if v is None:
            return
        self.visit = None
        self.db.touch_visit(v.id, v.last_seen, 0, 0.0, ())
        log.info("Camera %d: visit %d ended after %.0fs (%d photos)", self.cam.id, v.id,
                 v.last_seen - v.started_at, v.photos)
        self.bus.publish("visit_ended", {"camera_id": self.cam.id, "visit_id": v.id})

    # -- video ------------------------------------------------------------------------
    def _maybe_start_clip(self, now: float) -> None:
        vs = self.store.settings.video
        if (not vs.enabled or self.clip is not None or self.backend is None
                or now - self.last_clip_end < vs.cooldown or not self.media.can_write()):
            return
        path = self.media.new_video_path(self.cam.id, now)
        try:
            self.backend.start_clip(path, vs.bitrate)
        except Exception as e:
            log.error("Camera %d: could not start recording: %s", self.cam.id, e)
            return
        self.clip = Clip(path=self.media.rel(path), started_at=now,
                         visit_id=self.visit.id if self.visit else None)
        self.bus.publish("recording", {"camera_id": self.cam.id, "active": True})

    def _manage_clip(self, now: float, detected: bool) -> None:
        if self.clip is None:
            return
        vs = self.store.settings.video
        elapsed = now - self.clip.started_at
        quiet = self.visit is None or now - self.visit.last_seen > 3
        if elapsed >= vs.max_clip_seconds or (elapsed >= vs.clip_seconds and quiet) or not vs.enabled:
            self._stop_clip(now)

    def _stop_clip(self, now: float) -> None:
        clip, self.clip = self.clip, None
        if clip is None:
            return
        try:
            if self.backend is not None:
                self.backend.stop_clip()
        except Exception:
            log.exception("Camera %d: stop recording failed", self.cam.id)
        self.last_clip_end = now
        try:
            size = self.media.resolve(clip.path).stat().st_size
        except OSError:
            size = 0
        if size:
            self.db.add_video(visit_id=clip.visit_id, camera_id=self.cam.id, started_at=clip.started_at,
                              duration=round(now - clip.started_at, 1), path=clip.path, size_bytes=size)
        else:
            log.warning("Camera %d: recording %s is empty", self.cam.id, clip.path)
        self.bus.publish("recording", {"camera_id": self.cam.id, "active": False})

    # -- misc -------------------------------------------------------------------------
    def _count_frame(self, now: float) -> None:
        c = self.counters
        c.frames += 1
        c.window_frames += 1
        if now - c.window_start >= 10:
            c.fps = c.window_frames / (now - c.window_start)
            c.window_start, c.window_frames = now, 0
