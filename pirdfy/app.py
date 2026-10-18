"""Application wiring and lifecycle (independent of the web layer)."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from .auth import AuthStore
from .camera import CameraBackend, make_backend
from .config import CameraSettings, ConfigStore, Settings
from .db import Database
from .detector import Detector
from .events import EventBus
from .notify import Notifier
from .paths import Paths
from .pipeline import CameraWorker
from .storage import MediaStore
from .system import SystemMonitor, Updater

log = logging.getLogger(__name__)

HOUSEKEEPING_SECONDS = 15 * 60


class Pirdfy:
    def __init__(self, paths: Paths, *, detector: Detector | None = None,
                 backend_factory: Callable[[CameraSettings], CameraBackend] = make_backend):
        paths.ensure()
        self.paths = paths
        self.store = ConfigStore(paths.config)
        s = self.store.settings
        self.bus = EventBus()
        self.db = Database(paths.db)
        self.auth = AuthStore(paths.auth)
        self.media = MediaStore(paths.media, self.db, self.bus, s.storage)
        self.detector = detector or Detector(paths.models, s.detection)
        self.notifier = Notifier(s.notifications, paths.tmp)
        self.monitor = SystemMonitor()
        self.updater = Updater(paths.update_dir, s.updates.channel)
        self.backend_factory = backend_factory
        self.workers: dict[int, CameraWorker] = {}
        self._stop = threading.Event()
        self._housekeeper: threading.Thread | None = None
        self.store.on_change(self._on_settings)

    # -- lifecycle --------------------------------------------------------------------
    def start(self) -> None:
        if not self.detector.ready:
            self.detector.load()
        self.notifier.start()
        self._sync_workers(self.store.settings)
        self._housekeeper = threading.Thread(target=self._housekeeping_loop, name="housekeeping", daemon=True)
        self._housekeeper.start()
        log.info("Pirdfy started with %d camera(s)", len(self.workers))

    def stop(self) -> None:
        self._stop.set()
        for w in list(self.workers.values()):
            w.stop()
        self.workers.clear()
        self.notifier.stop()
        if self._housekeeper:
            self._housekeeper.join(timeout=5)
        self.db.close()
        log.info("Pirdfy stopped")

    def _housekeeping_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.media.housekeeping()
            except Exception:
                log.exception("Housekeeping failed")
            self._stop.wait(HOUSEKEEPING_SECONDS)

    # -- settings -----------------------------------------------------------------------
    def _on_settings(self, old: Settings, new: Settings) -> None:
        logging.getLogger().setLevel(new.log_level)
        self.detector.update_settings(new.detection)
        self.notifier.update_settings(new.notifications)
        self.media.settings = new.storage
        self.updater.channel = new.updates.channel
        self._sync_workers(new)
        if new.storage != old.storage:
            threading.Thread(target=self.media.housekeeping, daemon=True).start()

    def _sync_workers(self, settings: Settings) -> None:
        wanted = {c.id: c for c in settings.cameras if c.enabled}
        for cid in list(self.workers):
            if cid not in wanted:
                self.workers.pop(cid).stop()
        for cid, cam in wanted.items():
            if cid in self.workers:
                self.workers[cid].apply_settings(cam)
            elif not self._stop.is_set():
                w = CameraWorker(cam, store=self.store, db=self.db, media=self.media, detector=self.detector,
                                 notifier=self.notifier, bus=self.bus, backend_factory=self.backend_factory)
                self.workers[cid] = w
                w.start()

    # -- status -------------------------------------------------------------------------
    def warnings(self) -> list[dict]:
        out = []
        if not self.detector.ready:
            out.append({"code": "detector", "message": self.detector.error or "Detector not loaded"})
        for w in self.workers.values():
            if w.error:
                out.append({"code": "camera", "message": f"{w.cam.name}: {w.error}"})
        if self.media.low_space:
            out.append({"code": "low_space", "message": "Disk almost full: recording paused"})
        if not self.auth.has_password:
            out.append({"code": "no_password", "message": "No password set. Set one in Settings."})
        if self.notifier.last_error and self.store.settings.notifications.enabled:
            out.append({"code": "notify", "message": f"Notifications: {self.notifier.last_error}"})
        return out
