"""Push notifications via Apprise, sent from a background thread."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import NotificationSettings, format_template

log = logging.getLogger(__name__)


@dataclass
class VisitNotice:
    camera: str
    count: int
    label: str
    confidence: float
    ts: float
    image: bytes | None = None


class Notifier:
    def __init__(self, settings: NotificationSettings, tmp_dir: Path):
        self.settings = settings
        self.tmp_dir = Path(tmp_dir)
        self._queue: queue.Queue[tuple[VisitNotice | None, threading.Event | None, list]] = queue.Queue(8)
        self._last_sent = 0.0
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread:
            self._queue.put((None, None, []))
            self._thread.join(timeout=5)

    def update_settings(self, settings: NotificationSettings) -> None:
        self.settings = settings

    def visit_started(self, notice: VisitNotice) -> None:
        s = self.settings
        if not (s.enabled and s.urls):
            return
        if time.time() - self._last_sent < s.cooldown:
            return
        self._last_sent = time.time()
        try:
            self._queue.put_nowait((notice, None, []))
        except queue.Full:
            log.warning("Notification queue full; dropping")

    def send_test(self, timeout: float = 20) -> tuple[bool, str | None]:
        if not self.settings.urls:
            return False, "No notification URLs configured"
        done, result = threading.Event(), []
        notice = VisitNotice(camera="Test", count=1, label="bird", confidence=0.99, ts=time.time())
        self._queue.put((notice, done, result))
        if not done.wait(timeout):
            return False, "Timed out"
        return result[0], result[1]

    def _run(self) -> None:
        while True:
            notice, done, result = self._queue.get()
            if notice is None:
                return
            ok, err = self._send(notice, test=done is not None)
            if done is not None:
                result.extend([ok, err])
                done.set()

    def _send(self, n: VisitNotice, test: bool = False) -> tuple[bool, str | None]:
        try:
            import apprise
        except ImportError:
            self.last_error = "apprise is not installed"
            return False, self.last_error
        s = self.settings
        ap = apprise.Apprise()
        for url in s.urls:
            ap.add(url)
        if len(ap) == 0:
            self.last_error = "None of the notification URLs are valid"
            return False, self.last_error
        fields = {"time": datetime.fromtimestamp(n.ts).strftime("%H:%M"),
                  "date": datetime.fromtimestamp(n.ts).strftime("%Y-%m-%d"),
                  "count": n.count, "confidence": n.confidence, "camera": n.camera, "label": n.label}
        title = format_template(s.title, **fields)
        body = "Test notification from Pirdfy" if test else format_template(s.body, **fields)
        attach_path = None
        try:
            if n.image and s.attach_image:
                self.tmp_dir.mkdir(parents=True, exist_ok=True)
                attach_path = self.tmp_dir / f"notify_{int(n.ts * 1000)}.jpg"
                attach_path.write_bytes(n.image)
            ok = bool(ap.notify(title=title, body=body, attach=str(attach_path) if attach_path else None))
            self.last_error = None if ok else "Delivery failed (check URLs and network)"
            (log.info if ok else log.warning)("Notification %s", "sent" if ok else "failed")
            return ok, self.last_error
        except Exception as e:
            self.last_error = str(e)
            log.exception("Notification error")
            return False, self.last_error
        finally:
            if attach_path:
                attach_path.unlink(missing_ok=True)
