"""YOLO object detection with ONNX Runtime (no PyTorch at runtime).

The model is a YOLOv8/YOLO11 detection model exported to ONNX (output shape
``(1, 4 + num_classes, N)``). The installer fetches or exports it once.
"""

from __future__ import annotations

import ast
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import COCO_CLASSES, DetectionSettings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Detection:
    label: str
    confidence: float
    # Normalised to the analysed frame: 0..1, top-left origin.
    x: float
    y: float
    w: float
    h: float

    @property
    def area(self) -> float:
        return self.w * self.h

    def as_row(self) -> tuple[str, float, float, float, float, float]:
        return (self.label, round(self.confidence, 4), round(self.x, 5), round(self.y, 5),
                round(self.w, 5), round(self.h, 5))


def letterbox(img: np.ndarray, size: tuple[int, int]) -> tuple[np.ndarray, float, tuple[int, int]]:
    """Resize keeping aspect ratio and pad to ``size`` (h, w). Returns image, scale, (pad_x, pad_y)."""
    import cv2

    h, w = img.shape[:2]
    th, tw = size
    scale = min(th / h, tw / w)
    nh, nw = round(h * scale), round(w * scale)
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR) if (nh, nw) != (h, w) else img
    pad_y, pad_x = (th - nh) // 2, (tw - nw) // 2
    out = np.full((th, tw, 3), 114, dtype=np.uint8)
    out[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return out, scale, (pad_x, pad_y)


def nms(boxes: np.ndarray, scores: np.ndarray, iou: float) -> list[int]:
    """Greedy non-maximum suppression. boxes: (N, 4) as x1, y1, x2, y2."""
    order = scores.argsort()[::-1]
    keep: list[int] = []
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    while order.size:
        i = int(order[0])
        keep.append(i)
        rest = order[1:]
        xx1 = np.maximum(boxes[i, 0], boxes[rest, 0])
        yy1 = np.maximum(boxes[i, 1], boxes[rest, 1])
        xx2 = np.minimum(boxes[i, 2], boxes[rest, 2])
        yy2 = np.minimum(boxes[i, 3], boxes[rest, 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        overlap = inter / (areas[i] + areas[rest] - inter + 1e-9)
        order = rest[overlap <= iou]
    return keep


def postprocess(output: np.ndarray, *, class_ids: list[int], names: list[str], conf: float, iou: float,
                scale: float, pad: tuple[int, int], frame_size: tuple[int, int],
                min_area: float = 0.0) -> list[Detection]:
    """Decode raw YOLO output (1, 4+C, N) into detections normalised to the original frame."""
    pred = output[0].T  # (N, 4+C)
    scores_all = pred[:, 4:][:, class_ids]
    best = scores_all.argmax(axis=1)
    scores = scores_all[np.arange(len(pred)), best]
    mask = scores >= conf
    if not mask.any():
        return []
    pred, scores, best = pred[mask], scores[mask], best[mask]
    cx, cy, bw, bh = pred[:, 0], pred[:, 1], pred[:, 2], pred[:, 3]
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
    # Class-aware NMS by offsetting boxes per class.
    offset = best[:, None].astype(np.float32) * 10_000
    keep = nms(boxes + offset, scores, iou)
    fh, fw = frame_size
    out = []
    for i in keep:
        x1, y1, x2, y2 = boxes[i]
        x1 = (x1 - pad[0]) / scale
        x2 = (x2 - pad[0]) / scale
        y1 = (y1 - pad[1]) / scale
        y2 = (y2 - pad[1]) / scale
        x1, x2 = np.clip([x1, x2], 0, fw)
        y1, y2 = np.clip([y1, y2], 0, fh)
        w, h = (x2 - x1) / fw, (y2 - y1) / fh
        if w <= 0 or h <= 0 or w * h < min_area:
            continue
        out.append(Detection(names[class_ids[best[i]]], float(scores[i]), float(x1 / fw), float(y1 / fh),
                             float(w), float(h)))
    out.sort(key=lambda d: d.confidence, reverse=True)
    return out


class Detector:
    """Thread-safe wrapper around an ONNX Runtime session."""

    def __init__(self, model_dir: Path, settings: DetectionSettings):
        self.model_dir = Path(model_dir)
        self.settings = settings
        self.session = None
        self.error: str | None = None
        self.names: list[str] = list(COCO_CLASSES)
        self.input_name = "images"
        self.input_size = (640, 640)
        self.last_ms: float = 0.0
        self.runs = 0

    @property
    def model_path(self) -> Path:
        return self.model_dir / f"{self.settings.model}.onnx"

    @property
    def ready(self) -> bool:
        return self.session is not None

    def load(self) -> bool:
        path = self.model_path
        if not path.exists():
            self.error = f"Model file not found: {path}. Run 'sudo pirdfy model' to download it."
            log.error(self.error)
            return False
        try:
            import onnxruntime as ort

            if hasattr(ort, "disable_telemetry_events"):
                ort.disable_telemetry_events()  # stay private and offline
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = self.settings.threads
            opts.inter_op_num_threads = 1
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess = ort.InferenceSession(str(path), opts, providers=["CPUExecutionProvider"])
            inp = sess.get_inputs()[0]
            self.input_name = inp.name
            h, w = inp.shape[2], inp.shape[3]
            self.input_size = (int(h) if isinstance(h, int) else 640, int(w) if isinstance(w, int) else 640)
            meta = sess.get_modelmeta().custom_metadata_map
            if "names" in meta:
                parsed = ast.literal_eval(meta["names"])
                self.names = [parsed[i] for i in sorted(parsed)]
            self.session = sess
            self.error = None
            log.info("Loaded %s (input %sx%s, %d classes)", path.name, *self.input_size, len(self.names))
            return True
        except Exception as e:  # corrupt model, wrong format, ...
            self.error = f"Failed to load model {path.name}: {e}"
            log.exception("Model load failed")
            return False

    def update_settings(self, settings: DetectionSettings) -> None:
        reload = (settings.model != self.settings.model or settings.threads != self.settings.threads)
        self.settings = settings
        if reload:
            self.session = None
            self.load()

    def class_ids(self) -> list[int]:
        return [self.names.index(c) for c in self.settings.classes if c in self.names]

    def detect(self, rgb: np.ndarray) -> list[Detection]:
        """Run detection on an RGB uint8 frame."""
        sess = self.session
        if sess is None:
            return []
        t0 = time.perf_counter()
        img, scale, pad = letterbox(rgb, self.input_size)
        blob = np.ascontiguousarray(img.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
        output = sess.run(None, {self.input_name: blob})[0]
        s = self.settings
        dets = postprocess(output, class_ids=self.class_ids(), names=self.names, conf=s.confidence,
                           iou=s.iou, scale=scale, pad=pad, frame_size=rgb.shape[:2],
                           min_area=s.min_box_area)
        self.last_ms = (time.perf_counter() - t0) * 1000
        self.runs += 1
        return dets

    def info(self) -> dict:
        return {"model": self.settings.model, "ready": self.ready, "error": self.error,
                "input_size": list(self.input_size), "classes": self.settings.classes,
                "confidence": self.settings.confidence, "last_ms": round(self.last_ms, 1),
                "runs": self.runs}
