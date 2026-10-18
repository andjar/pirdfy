import os
from pathlib import Path

import numpy as np
import pytest

from pirdfy.config import COCO_CLASSES, DetectionSettings
from pirdfy.detector import Detector, letterbox, nms, postprocess

BIRD = COCO_CLASSES.index("bird")


def raw_output(boxes):
    """Build a fake YOLO output (1, 84, N) from (cx, cy, w, h, class_id, score)."""
    out = np.zeros((1, 84, max(len(boxes), 1)), np.float32)
    for i, (cx, cy, w, h, cls, score) in enumerate(boxes):
        out[0, :4, i] = (cx, cy, w, h)
        out[0, 4 + cls, i] = score
    return out


def test_letterbox_geometry():
    img = np.zeros((360, 640, 3), np.uint8)
    out, scale, pad = letterbox(img, (384, 640))
    assert out.shape == (384, 640, 3)
    assert scale == 1.0 and pad == (0, 12)
    out, scale, pad = letterbox(np.zeros((1080, 1920, 3), np.uint8), (640, 640))
    assert scale == pytest.approx(1 / 3) and pad == (0, 140)


def test_postprocess_filters_class_and_maps_back():
    out = raw_output([
        (320, 192, 64, 64, BIRD, 0.9),         # bird in the middle
        (100, 100, 20, 20, 0, 0.95),           # a person: not requested
        (322, 194, 60, 60, BIRD, 0.6),         # duplicate of the first bird
        (500, 200, 40, 40, BIRD, 0.3),         # below threshold
    ])
    dets = postprocess(out, class_ids=[BIRD], names=list(COCO_CLASSES), conf=0.45, iou=0.5, scale=1.0,
                       pad=(0, 12), frame_size=(360, 640))
    assert len(dets) == 1
    d = dets[0]
    assert d.label == "bird" and d.confidence == pytest.approx(0.9)
    assert d.x == pytest.approx((320 - 32) / 640)
    assert d.y == pytest.approx((192 - 32 - 12) / 360)
    assert d.w == pytest.approx(64 / 640)


def test_postprocess_min_area():
    out = raw_output([(320, 192, 4, 4, BIRD, 0.9)])
    assert postprocess(out, class_ids=[BIRD], names=list(COCO_CLASSES), conf=0.45, iou=0.5, scale=1.0,
                       pad=(0, 12), frame_size=(360, 640), min_area=0.001) == []


def test_nms_keeps_separate_boxes():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], np.float32)
    keep = nms(boxes, np.array([0.9, 0.8, 0.7]), 0.5)
    assert sorted(keep) == [0, 2]


def test_missing_model_reports_error(tmp_path):
    det = Detector(tmp_path, DetectionSettings())
    assert det.load() is False
    assert "not found" in det.error
    assert det.detect(np.zeros((360, 640, 3), np.uint8)) == []


MODEL_DIR = os.environ.get("PIRDFY_TEST_MODEL_DIR")


@pytest.mark.skipif(not MODEL_DIR, reason="set PIRDFY_TEST_MODEL_DIR to a folder with yolov8n.onnx")
def test_real_model_finds_people():
    import cv2

    img_path = Path(os.environ.get("PIRDFY_TEST_IMAGE", ""))
    if not img_path.exists():
        pytest.skip("set PIRDFY_TEST_IMAGE to a photo with people (e.g. ultralytics bus.jpg)")
    det = Detector(Path(MODEL_DIR), DetectionSettings(classes=["person"]))
    assert det.load(), det.error
    rgb = cv2.cvtColor(cv2.imread(str(img_path)), cv2.COLOR_BGR2RGB)
    dets = det.detect(cv2.resize(rgb, (640, 360)))
    assert len(dets) >= 3 and all(d.label == "person" for d in dets)
