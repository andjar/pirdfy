"""Camera backends.

One always-on configuration per camera (no reconfiguring while running):
  * ``main``  – full resolution YUV420, used for saved photos and H.264 video
  * ``lores`` – ~640 px YUV420 from the ISP, used for motion and detection

Video is encoded from ``main`` with ``start_encoder``/``stop_encoder`` while the
camera keeps streaming, so analysis never stops during a recording. On the Pi 5
picamera2's H264Encoder is libav (software) H.264, muxed straight to MP4.

All arrays handed to the rest of the app are RGB (uint8, HxWx3).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from .config import CameraSettings

log = logging.getLogger(__name__)

LORES_WIDTH = 640


class CameraError(RuntimeError):
    pass


def lores_size(main: tuple[int, int]) -> tuple[int, int]:
    w, h = main
    lh = round(LORES_WIDTH * h / w / 2) * 2
    return (LORES_WIDTH, lh)


def yuv420_to_rgb(arr: np.ndarray, width: int, height: int) -> np.ndarray:
    """Convert a picamera2 YUV420 (I420) buffer, possibly row-padded, to RGB."""
    import cv2

    rgb = cv2.cvtColor(arr, cv2.COLOR_YUV2RGB_I420)
    if rgb.shape[1] != width or rgb.shape[0] != height:
        rgb = np.ascontiguousarray(rgb[:height, :width])
    return rgb


class Frame:
    """Lazy accessors for one captured frame; only converts what is asked for."""

    def __init__(self, ts: float, gray, lores, main):
        self.ts = ts
        self._gray, self._lores, self._main = gray, lores, main
        self._cache: dict[str, np.ndarray] = {}

    def _get(self, key: str, fn) -> np.ndarray:
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def gray(self) -> np.ndarray:
        return self._get("gray", self._gray)

    def lores_rgb(self) -> np.ndarray:
        return self._get("lores", self._lores)

    def main_rgb(self) -> np.ndarray:
        return self._get("main", self._main)


class CameraBackend(ABC):
    def __init__(self, settings: CameraSettings):
        self.settings = settings
        self.recording = False
        self.model = "unknown"

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    @contextmanager
    def capture(self) -> Iterator[Frame]: ...

    @abstractmethod
    def apply_controls(self, settings: CameraSettings) -> None: ...

    @abstractmethod
    def start_clip(self, path: Path, bitrate: int) -> None: ...

    @abstractmethod
    def stop_clip(self) -> None: ...


# --------------------------------------------------------------------------------------
class Picamera2Backend(CameraBackend):
    def __init__(self, settings: CameraSettings):
        super().__init__(settings)
        self.cam = None
        self._encoder = None
        self._lock = threading.Lock()

    def open(self) -> None:
        try:
            from libcamera import Transform
            from picamera2 import Picamera2
        except ImportError as e:  # pragma: no cover - only on a Pi
            raise CameraError(f"picamera2 is not installed ({e}). Install python3-picamera2.") from e
        s = self.settings
        infos = Picamera2.global_camera_info()
        if s.id >= len(infos):
            raise CameraError(f"Camera {s.id} not detected (found {len(infos)}). Check the ribbon cable.")
        self.model = infos[s.id].get("Model", "unknown")
        cam = Picamera2(camera_num=s.id)
        try:
            lores = lores_size(s.resolution)
            config = cam.create_video_configuration(
                main={"size": tuple(s.resolution), "format": "YUV420"},
                lores={"size": lores, "format": "YUV420"},
                transform=Transform(hflip=int(s.hflip), vflip=int(s.vflip)),
                controls={"FrameRate": float(s.fps)},
                buffer_count=6,
            )
            cam.configure(config)
            self.cam = cam
            self._main_size = config["main"]["size"]
            self._lores_size = config["lores"]["size"]
            self.apply_controls(s)
            cam.start()
        except Exception:
            cam.close()
            self.cam = None
            raise
        log.info("Camera %d (%s) started: main %s lores %s @ %d fps", s.id, self.model,
                 self._main_size, self._lores_size, s.fps)

    def close(self) -> None:
        with self._lock:
            if self.cam is None:
                return
            try:
                if self.recording:
                    self._stop_encoder()
                self.cam.stop()
            finally:
                self.cam.close()
                self.cam = None

    @contextmanager
    def capture(self) -> Iterator[Frame]:
        if self.cam is None:
            raise CameraError("camera not open")
        request = self.cam.capture_request()
        try:
            ts = time.time()
            lw, lh = self._lores_size
            mw, mh = self._main_size
            lores_buf = request.make_array("lores")  # small copy (~0.3 MB)
            frame = Frame(
                ts,
                gray=lambda: lores_buf[:lh, :lw],
                lores=lambda: yuv420_to_rgb(lores_buf, lw, lh),
                main=lambda: yuv420_to_rgb(request.make_array("main"), mw, mh),
            )
            yield frame
        finally:
            request.release()

    def apply_controls(self, settings: CameraSettings) -> None:
        from libcamera import controls as lc

        self.settings = settings
        if self.cam is None:
            return
        available = self.cam.camera_controls
        c: dict = {}
        if settings.exposure_mode == "auto":
            c["AeEnable"] = True
            c["ExposureValue"] = settings.exposure_compensation
        else:
            c["AeEnable"] = False
            c["ExposureTime"] = settings.exposure_time_us
        awb = {"auto": "Auto", "incandescent": "Incandescent", "tungsten": "Tungsten",
               "fluorescent": "Fluorescent", "indoor": "Indoor", "daylight": "Daylight",
               "cloudy": "Cloudy"}[settings.awb_mode]
        c["AwbEnable"] = True
        c["AwbMode"] = getattr(lc.AwbModeEnum, awb)
        c["Brightness"] = settings.brightness
        c["Contrast"] = settings.contrast
        c["Saturation"] = settings.saturation
        c["Sharpness"] = settings.sharpness
        if "AfMode" in available:  # Camera Module 3
            if settings.autofocus == "continuous":
                c["AfMode"] = lc.AfModeEnum.Continuous
            else:
                c["AfMode"] = lc.AfModeEnum.Manual
                c["LensPosition"] = settings.lens_position
        c = {k: v for k, v in c.items() if k in available}
        self.cam.set_controls(c)

    def start_clip(self, path: Path, bitrate: int) -> None:
        from picamera2.encoders import H264Encoder

        try:
            from picamera2.outputs import PyavOutput

            output = PyavOutput(str(path), format="mp4")
        except ImportError:  # older picamera2
            from picamera2.outputs import FfmpegOutput

            output = FfmpegOutput(str(path))
        with self._lock:
            if self.cam is None or self.recording:
                return
            encoder = H264Encoder(bitrate=bitrate)
            self.cam.start_encoder(encoder, output, name="main")
            self._encoder = encoder
            self.recording = True

    def _stop_encoder(self) -> None:
        if self._encoder is not None:
            self.cam.stop_encoder(self._encoder)
            self._encoder = None
        self.recording = False

    def stop_clip(self) -> None:
        with self._lock:
            if self.cam is not None and self.recording:
                self._stop_encoder()


# --------------------------------------------------------------------------------------
class MockBackend(CameraBackend):
    """Development backend. Only used when explicitly selected (PIRDFY_CAMERA=mock).

    With PIRDFY_MOCK_IMAGES=<dir>, cycles through the JPEGs in that folder, showing
    each for a few seconds with empty background frames in between.
    """

    def __init__(self, settings: CameraSettings):
        super().__init__(settings)
        self.model = "mock"
        self._images: list[np.ndarray] = []
        self._start = time.time()
        self._writer = None
        self._open = False

    def open(self) -> None:
        import cv2

        folder = os.environ.get("PIRDFY_MOCK_IMAGES")
        w, h = self.settings.resolution
        if folder:
            for p in sorted(Path(folder).glob("*.jp*g")):
                img = cv2.imread(str(p))
                if img is not None:
                    self._images.append(cv2.resize(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), (w, h)))
        rng = np.random.default_rng(0)
        base = np.linspace(60, 140, w, dtype=np.float32)[None, :, None] * np.ones((h, 1, 3), np.float32)
        noise = rng.normal(0, 4, (h, w, 3)).astype(np.float32)
        self._background = np.clip(base + noise, 0, 255).astype(np.uint8)
        self._background[..., 1] = np.clip(self._background[..., 1] + 30, 0, 255)
        self._open = True

    def close(self) -> None:
        self.stop_clip()
        self._open = False

    def _render(self, t: float) -> np.ndarray:
        if self._images:
            slot = int(t // 4)
            if slot % 3 == 2:  # every third slot: empty scene
                return self._background
            return self._images[(slot // 3 * 2 + slot % 3) % len(self._images)]
        img = self._background.copy()
        h, w = img.shape[:2]
        if int(t) % 20 < 8:  # a "bird" visits for 8 s every 20 s
            cx = int(w * (0.3 + 0.1 * np.sin(t)))
            cy = int(h * 0.55)
            import cv2

            cv2.ellipse(img, (cx, cy), (w // 18, h // 22), 0, 0, 360, (120, 70, 40), -1)
        return img

    @contextmanager
    def capture(self) -> Iterator[Frame]:
        import cv2

        if not self._open:
            raise CameraError("camera not open")
        time.sleep(0.01)
        main = self._render(time.time() - self._start)
        lw, lh = lores_size(self.settings.resolution)
        lores = cv2.resize(main, (lw, lh), interpolation=cv2.INTER_AREA)
        if self._writer is not None:
            self._writer.write(cv2.cvtColor(lores, cv2.COLOR_RGB2BGR))
        yield Frame(time.time(), gray=lambda: cv2.cvtColor(lores, cv2.COLOR_RGB2GRAY),
                    lores=lambda: lores, main=lambda: main)

    def apply_controls(self, settings: CameraSettings) -> None:
        self.settings = settings

    def start_clip(self, path: Path, bitrate: int) -> None:
        import cv2

        lw, lh = lores_size(self.settings.resolution)
        self._writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 2, (lw, lh))
        self.recording = True

    def stop_clip(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
        self.recording = False


def make_backend(settings: CameraSettings) -> CameraBackend:
    kind = os.environ.get("PIRDFY_CAMERA", "picamera2").lower()
    if kind == "mock":
        return MockBackend(settings)
    if kind != "picamera2":
        raise CameraError(f"Unknown PIRDFY_CAMERA={kind!r} (use picamera2 or mock)")
    return Picamera2Backend(settings)
