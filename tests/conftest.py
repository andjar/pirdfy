from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pytest

from pirdfy.camera import CameraBackend, Frame
from pirdfy.config import CameraSettings, DetectionSettings
from pirdfy.detector import Detection
from pirdfy.paths import Paths


class FakeClock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeBackend(CameraBackend):
    """Frames are flat images; the clock decides the timestamp."""

    def __init__(self, settings: CameraSettings, clock: FakeClock | None = None):
        super().__init__(settings)
        self.clock = clock or FakeClock()
        self.clips: list[Path] = []
        self.opened = False
        self.value = 100

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False

    @contextmanager
    def capture(self):
        main = np.full((360, 640, 3), self.value, np.uint8)
        yield Frame(self.clock(), gray=lambda: main[..., 0], lores=lambda: main, main=lambda: main)

    def apply_controls(self, settings):
        self.settings = settings

    def start_clip(self, path, bitrate):
        Path(path).write_bytes(b"\x00" * 100)
        self.clips.append(Path(path))
        self.recording = True

    def stop_clip(self):
        self.recording = False


class FakeDetector:
    """Returns whatever is queued in ``script`` (one entry per call)."""

    def __init__(self):
        self.settings = DetectionSettings()
        self.script: list[list[Detection]] = []
        self.ready = True
        self.error = None
        self.calls = 0

    def detect(self, rgb):
        self.calls += 1
        return self.script.pop(0) if self.script else []

    def load(self):
        return True

    def update_settings(self, s):
        self.settings = s

    def info(self):
        return {"model": "fake", "ready": True, "error": None, "input_size": [384, 640], "classes": ["bird"],
                "confidence": 0.45, "last_ms": 1.0, "runs": self.calls}


def bird(conf: float = 0.9, x: float = 0.4) -> Detection:
    return Detection("bird", conf, x, 0.4, 0.1, 0.12)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(data=tmp_path / "data", config=tmp_path / "config.yaml")
    p.ensure()
    return p
