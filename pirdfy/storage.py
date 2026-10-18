"""Media files, retention and disk-space protection."""

from __future__ import annotations

import io
import logging
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import StorageSettings
from .db import Database
from .events import EventBus

log = logging.getLogger(__name__)

THUMB_WIDTH = 480
GB = 1024 ** 3


def encode_jpeg(rgb: np.ndarray, quality: int = 88, width: int | None = None) -> bytes:
    from PIL import Image

    img = Image.fromarray(rgb)
    if width and img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=False)
    return buf.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)


class MediaStore:
    def __init__(self, root: Path, db: Database, bus: EventBus, settings: StorageSettings):
        self.root = Path(root)
        self.db = db
        self.bus = bus
        self.settings = settings
        self._lock = threading.Lock()
        self.low_space = False

    # -- paths ------------------------------------------------------------------------
    def _day_dir(self, ts: float) -> Path:
        d = self.root / datetime.fromtimestamp(ts).strftime("%Y/%m/%d")
        d.mkdir(parents=True, exist_ok=True)
        return d

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def resolve(self, rel_path: str) -> Path:
        """Map a DB path to disk, refusing anything outside the media root."""
        p = (self.root / rel_path).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("path outside media root")
        return p

    # -- writing ----------------------------------------------------------------------
    def save_photo(self, rgb: np.ndarray, *, camera_id: int, ts: float, quality: int) -> dict:
        stem = f"cam{camera_id}_{datetime.fromtimestamp(ts).strftime('%H%M%S_%f')[:-3]}"
        d = self._day_dir(ts)
        full, thumb = d / f"{stem}.jpg", d / f"{stem}_t.jpg"
        data = encode_jpeg(rgb, quality)
        tdata = encode_jpeg(rgb, 80, THUMB_WIDTH)
        _write_atomic(full, data)
        _write_atomic(thumb, tdata)
        h, w = rgb.shape[:2]
        return {"path": self.rel(full), "thumb_path": self.rel(thumb), "width": w, "height": h,
                "size_bytes": len(data) + len(tdata)}

    def new_video_path(self, camera_id: int, ts: float) -> Path:
        stem = f"cam{camera_id}_{datetime.fromtimestamp(ts).strftime('%H%M%S')}"
        return self._day_dir(ts) / f"{stem}.mp4"

    # -- deleting ---------------------------------------------------------------------
    def _unlink(self, rel_path: str) -> None:
        try:
            self.resolve(rel_path).unlink(missing_ok=True)
        except (OSError, ValueError) as e:
            log.warning("Could not delete %s: %s", rel_path, e)

    def delete_visit(self, visit_id: int) -> None:
        for p in self.db.visit_files(visit_id):
            self._unlink(p)
        self.db.delete_visit(visit_id)

    # -- housekeeping -------------------------------------------------------------------
    def disk(self) -> dict:
        usage = shutil.disk_usage(self.root)
        return {"total_gb": round(usage.total / GB, 1), "free_gb": round(usage.free / GB, 2),
                "media_gb": round(self.db.media_bytes() / GB, 2), "low_space": self.low_space}

    def can_write(self) -> bool:
        return not self.low_space

    def housekeeping(self) -> dict:
        """Apply retention, then free space. Safe to call often."""
        with self._lock:
            s = self.settings
            now = time.time()
            removed = {"visits": 0, "videos": 0}
            if s.video_retention_days:
                for v in self.db.videos_older_than(now - s.video_retention_days * 86400):
                    self._unlink(v["path"])
                    self.db.delete_video(v["id"])
                    removed["videos"] += 1
            if s.photo_retention_days:
                while ids := self.db.visits_older_than(now - s.photo_retention_days * 86400):
                    for vid in ids:
                        self.delete_visit(vid)
                    removed["visits"] += len(ids)
            self.db.drop_empty_visits(now - 3600)
            removed["visits"] += self._enforce_space()
            self._clean_tmp()
            if any(removed.values()):
                log.info("Housekeeping removed %d visits and %d videos", removed["visits"], removed["videos"])
            return removed

    def _over_budget(self) -> bool:
        s = self.settings
        free = shutil.disk_usage(self.root).free / GB
        over_cap = bool(s.max_storage_gb) and self.db.media_bytes() / GB > s.max_storage_gb
        return free < s.min_free_gb or over_cap

    def _enforce_space(self) -> int:
        removed = 0
        while self._over_budget():
            ids = self.db.oldest_visits(limit=50)
            if not ids:
                break
            for vid in ids:  # oldest first, re-checking after each (statvfs is cheap)
                self.delete_visit(vid)
                removed += 1
                if not self._over_budget():
                    break
        free = shutil.disk_usage(self.root).free / GB
        low = free < self.settings.min_free_gb / 2
        if low != self.low_space:
            self.low_space = low
            self.bus.publish("warning", {"code": "low_space", "active": low, "free_gb": round(free, 2)})
            (log.error if low else log.info)("Low disk space: %s (%.2f GB free)", low, free)
        return removed

    def _clean_tmp(self) -> None:
        cutoff = time.time() - 3600
        for p in self.root.rglob("*.part"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
