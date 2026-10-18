"""Typed configuration.

Defaults live in the pydantic models below (single source of truth). The YAML
file at ``PIRDFY_CONFIG`` (``/etc/pirdfy/config.yaml`` when installed) only holds
overrides, so updates can change defaults without touching user settings.
Changes made in the web UI are written back to that same file, atomically.
"""

from __future__ import annotations

import copy
import logging
import os
import string
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

log = logging.getLogger(__name__)

COCO_CLASSES: tuple[str, ...] = (
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush",
)

AWB_MODES = ("auto", "incandescent", "tungsten", "fluorescent", "indoor", "daylight", "cloudy")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class CameraSettings(_Model):
    id: Annotated[int, Field(ge=0, le=1)] = 0
    name: Annotated[str, Field(min_length=1, max_length=40)] = "Feeder"
    enabled: bool = True
    # Full-resolution stream: used for saved photos and video.
    resolution: tuple[int, int] = (1920, 1080)
    fps: Annotated[int, Field(ge=5, le=60)] = 30
    hflip: bool = False
    vflip: bool = False
    exposure_mode: Literal["auto", "manual"] = "auto"
    exposure_time_us: Annotated[int, Field(ge=100, le=1_000_000)] = 10_000
    exposure_compensation: Annotated[float, Field(ge=-4, le=4)] = 0.0
    awb_mode: Literal[AWB_MODES] = "auto"  # type: ignore[valid-type]
    brightness: Annotated[float, Field(ge=-1, le=1)] = 0.0
    contrast: Annotated[float, Field(ge=0, le=4)] = 1.0
    saturation: Annotated[float, Field(ge=0, le=4)] = 1.0
    sharpness: Annotated[float, Field(ge=0, le=8)] = 1.0
    # Camera Module 3 only; ignored by fixed-focus modules.
    autofocus: Literal["continuous", "manual"] = "continuous"
    lens_position: Annotated[float, Field(ge=0, le=15)] = 1.0

    @field_validator("resolution")
    @classmethod
    def _even_resolution(cls, v: tuple[int, int]) -> tuple[int, int]:
        w, h = v
        if not (320 <= w <= 4608 and 240 <= h <= 2592):
            raise ValueError("resolution out of range")
        return (w - w % 2, h - h % 2)

    # Fields that need the camera to be reconfigured (not just new controls).
    RESTART_FIELDS: ClassVar[frozenset[str]] = frozenset({"resolution", "fps", "hflip", "vflip", "enabled"})


class MotionSettings(_Model):
    enabled: bool = True
    # Per-pixel brightness change (0-255) that counts as "changed".
    pixel_threshold: Annotated[int, Field(ge=5, le=100)] = 25
    # Fraction of the frame that must change to wake the detector.
    area_threshold: Annotated[float, Field(ge=0.0001, le=0.5)] = 0.003
    # Run the detector at least this often even without motion (catches birds sitting still).
    idle_detect_seconds: Annotated[float, Field(ge=5, le=600)] = 30


class CaptureSettings(_Model):
    # Seconds between analysed frames.
    interval: Annotated[float, Field(ge=0.2, le=60)] = 0.5
    jpeg_quality: Annotated[int, Field(ge=50, le=100)] = 88
    # During a visit, save at most one photo this often (plus whenever more birds appear).
    photo_interval: Annotated[float, Field(ge=1, le=600)] = 5
    max_photos_per_visit: Annotated[int, Field(ge=1, le=500)] = 30
    # A visit ends after this many seconds without a detection.
    visit_gap: Annotated[float, Field(ge=3, le=600)] = 20
    motion: MotionSettings = MotionSettings()


class DetectionSettings(_Model):
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+$")] = "yolov8n"
    confidence: Annotated[float, Field(ge=0.05, le=0.99)] = 0.45
    iou: Annotated[float, Field(ge=0.1, le=0.95)] = 0.5
    classes: list[str] = ["bird"]
    # Ignore boxes smaller than this fraction of the frame (noise, distant birds).
    min_box_area: Annotated[float, Field(ge=0, le=0.5)] = 0.0005
    threads: Annotated[int, Field(ge=1, le=4)] = 2

    @field_validator("classes")
    @classmethod
    def _known_classes(cls, v: list[str]) -> list[str]:
        unknown = [c for c in v if c not in COCO_CLASSES]
        if unknown:
            raise ValueError(f"unknown classes: {unknown}")
        if not v:
            raise ValueError("at least one class is required")
        return list(dict.fromkeys(v))


class VideoSettings(_Model):
    enabled: bool = True
    clip_seconds: Annotated[int, Field(ge=5, le=300)] = 20
    # Keep recording while the visit continues, up to this length.
    max_clip_seconds: Annotated[int, Field(ge=5, le=600)] = 60
    cooldown: Annotated[int, Field(ge=0, le=3600)] = 10
    bitrate: Annotated[int, Field(ge=500_000, le=25_000_000)] = 6_000_000
    # Seconds of video from *before* the detection (the arrival). 0 = off.
    # Needs the H.264 encoder running all the time: on the Pi 5 that is software
    # encoding, so it costs CPU and heat. Measure with `pirdfy bench` first.
    preroll_seconds: Annotated[int, Field(ge=0, le=10)] = 0
    # CPU threads for the always-on encoder (leaves the rest for the detector).
    preroll_threads: Annotated[int, Field(ge=1, le=4)] = 2

    # Changing these restarts the camera (the encoder is set up when it opens).
    RESTART_FIELDS: ClassVar[frozenset[str]] = frozenset({"preroll_seconds", "preroll_threads", "bitrate"})

    @model_validator(mode="after")
    def _max_ge_clip(self) -> VideoSettings:
        if self.max_clip_seconds < self.clip_seconds:
            raise ValueError("Maximum clip length must be at least the clip length")
        return self


class StorageSettings(_Model):
    photo_retention_days: Annotated[int, Field(ge=0, le=3650)] = 30  # 0 = keep forever
    video_retention_days: Annotated[int, Field(ge=0, le=3650)] = 14
    max_storage_gb: Annotated[float, Field(ge=0)] = 0  # 0 = no cap
    min_free_gb: Annotated[float, Field(ge=0.2)] = 2.0


_SAMPLE_FIELDS = {"time": "12:00", "date": "2026-01-01", "count": 1, "confidence": 0.9,
                  "camera": "Feeder", "label": "bird"}


class NotificationSettings(_Model):
    enabled: bool = False
    urls: list[str] = []
    cooldown: Annotated[int, Field(ge=0, le=86_400)] = 300
    attach_image: bool = True
    title: Annotated[str, Field(max_length=200)] = "Bird at the feeder"
    body: Annotated[str, Field(max_length=1000)] = "{count} {label}(s) on {camera} at {time} ({confidence:.0%})"

    @field_validator("title", "body")
    @classmethod
    def _valid_template(cls, v: str) -> str:
        try:
            v.format(**_SAMPLE_FIELDS)
        except (KeyError, ValueError, IndexError) as e:
            fields = ", ".join("{" + k + "}" for k in _SAMPLE_FIELDS)
            raise ValueError(f"invalid template ({e}); available: {fields}") from None
        return v

    @field_validator("urls")
    @classmethod
    def _valid_urls(cls, v: list[str]) -> list[str]:
        out = [u.strip() for u in v if u.strip()]
        for u in out:
            if "://" not in u:
                raise ValueError(f"not an Apprise URL: {u[:12]}...")
        return out


class WebSettings(_Model):
    host: str = "0.0.0.0"
    port: Annotated[int, Field(ge=1, le=65535)] = 8080
    # Optional HTTPS. Strongly recommended if the dashboard is reachable from the internet.
    tls_cert: str | None = None
    tls_key: str | None = None
    session_days: Annotated[int, Field(ge=1, le=365)] = 30
    # Trust X-Forwarded-* headers from these proxies (e.g. "127.0.0.1" behind Caddy/nginx).
    trusted_proxies: list[str] = []


class UpdateSettings(_Model):
    channel: Literal["stable", "edge"] = "stable"
    check: bool = True


class Settings(_Model):
    cameras: list[CameraSettings] = [CameraSettings()]
    capture: CaptureSettings = CaptureSettings()
    detection: DetectionSettings = DetectionSettings()
    video: VideoSettings = VideoSettings()
    storage: StorageSettings = StorageSettings()
    notifications: NotificationSettings = NotificationSettings()
    web: WebSettings = WebSettings()
    updates: UpdateSettings = UpdateSettings()
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("cameras")
    @classmethod
    def _unique_cameras(cls, v: list[CameraSettings]) -> list[CameraSettings]:
        ids = [c.id for c in v]
        if len(ids) != len(set(ids)):
            raise ValueError("camera ids must be unique")
        if not 1 <= len(v) <= 2:
            raise ValueError("configure one or two cameras")
        return sorted(v, key=lambda c: c.id)


def deep_merge(base: dict, override: dict) -> dict:
    """Return base updated recursively with override (lists are replaced)."""
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _diff(current: Any, default: Any) -> Any:
    """Keep only values that differ from defaults (lists are kept whole)."""
    if isinstance(current, dict) and isinstance(default, dict):
        out = {}
        for k, v in current.items():
            if k not in default:
                out[k] = v
                continue
            d = _diff(v, default[k])
            if d is not _SAME:
                out[k] = d
        return out if out else _SAME
    return _SAME if current == default else current


_SAME = object()

MASK = "••••••"


def mask_secret_url(url: str) -> str:
    scheme, _, rest = url.partition("://")
    return f"{scheme}://{MASK}" if rest else MASK


class ConfigStore:
    """Thread-safe holder of the current Settings, persisted as YAML overrides."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Settings, Settings], None]] = []
        self.settings = self._load()

    def _load(self) -> Settings:
        if not self.path.exists():
            log.info("No config at %s, using defaults", self.path)
            return Settings()
        raw = yaml.safe_load(self.path.read_text()) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{self.path}: top level must be a mapping")
        return Settings.model_validate(raw)

    def on_change(self, fn: Callable[[Settings, Settings], None]) -> None:
        self._listeners.append(fn)

    def to_public_dict(self) -> dict:
        data = self.settings.model_dump(mode="json")
        data["notifications"]["urls"] = [mask_secret_url(u) for u in data["notifications"]["urls"]]
        return data

    def update(self, patch: dict) -> Settings:
        """Validate a partial update, persist it and notify listeners."""
        with self._lock:
            old = self.settings
            current = old.model_dump(mode="json")
            urls = patch.get("notifications", {}).get("urls")
            if urls is not None:
                # Masked entries mean "unchanged"; match by position.
                old_urls = current["notifications"]["urls"]
                patch = copy.deepcopy(patch)
                patch["notifications"]["urls"] = [
                    old_urls[i] if MASK in u and i < len(old_urls) else u for i, u in enumerate(urls)
                ]
            if "cameras" in patch:
                patch = copy.deepcopy(patch)
                by_id = {c["id"]: c for c in current["cameras"]}
                patch["cameras"] = [deep_merge(by_id.get(c.get("id", 0), {}), c) for c in patch["cameras"]]
            new = Settings.model_validate(deep_merge(current, patch))
            self._save(new)
            self.settings = new
        for fn in self._listeners:
            try:
                fn(old, new)
            except Exception:
                log.exception("config listener failed")
        return new

    def _save(self, settings: Settings) -> None:
        defaults = Settings().model_dump(mode="json")
        overrides = _diff(settings.model_dump(mode="json"), defaults)
        if overrides is _SAME:
            overrides = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        header = ("# Pirdfy configuration (overrides only; defaults are in pirdfy/config.py).\n"
                  "# Edited by the web UI as well; comments are not preserved.\n")
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(header)
                yaml.safe_dump(overrides, f, sort_keys=False, allow_unicode=True)
            os.chmod(tmp, 0o640)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def format_template(template: str, **fields: Any) -> str:
    """Format a user template, leaving unknown placeholders intact."""

    class _Safe(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return string.Formatter().vformat(template, (), _Safe(fields))
    except (ValueError, IndexError):
        return template
