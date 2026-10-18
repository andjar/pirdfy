import time

import pytest

from pirdfy.config import ConfigStore
from pirdfy.db import Database
from pirdfy.events import EventBus
from pirdfy.notify import Notifier
from pirdfy.pipeline import CameraWorker
from pirdfy.storage import MediaStore

from .conftest import FakeBackend, FakeClock, FakeDetector, bird


@pytest.fixture
def rig(paths):
    store = ConfigStore(paths.config)
    store.update({"capture": {"photo_interval": 5, "visit_gap": 20, "motion": {"enabled": False}},
                  "video": {"clip_seconds": 10, "max_clip_seconds": 30, "cooldown": 15}})
    db = Database(paths.db)
    bus = EventBus()
    media = MediaStore(paths.media, db, bus, store.settings.storage)
    det = FakeDetector()
    notifier = Notifier(store.settings.notifications, paths.tmp)
    sent = []
    notifier.visit_started = sent.append
    clock = FakeClock()
    worker = CameraWorker(store.settings.cameras[0], store=store, db=db, media=media, detector=det,
                          notifier=notifier, bus=bus, backend_factory=lambda s: FakeBackend(s, clock), clock=clock)
    worker._open()

    class Rig:
        pass

    r = Rig()
    r.store, r.db, r.media, r.det, r.worker, r.clock, r.sent = store, db, media, det, worker, clock, sent

    def frame(dets=None, dt=1.0):
        r.clock.t += dt
        r.det.script.append(dets or [])
        worker.step()

    r.frame = frame
    return r


def test_visit_lifecycle_saves_throttled_photos(rig):
    rig.frame([bird(0.7)])
    v = rig.worker.visit
    assert v is not None and rig.db.photo_count(v.id) == 1
    for _ in range(4):  # same bird, within photo_interval
        rig.frame([bird(0.8)])
    assert rig.db.photo_count(v.id) == 1
    rig.frame([bird(0.9)])  # 5 s after the first photo
    assert rig.db.photo_count(v.id) == 2
    rig.frame([bird(0.6), bird(0.5, x=0.1)])  # a second bird arrives: photo right away
    assert rig.db.photo_count(v.id) == 3
    # Visit continues through short gaps, ends after visit_gap seconds.
    rig.frame([], dt=10)
    assert rig.worker.visit is not None
    rig.frame([], dt=11)
    assert rig.worker.visit is None
    row = rig.db.get_visit(v.id)
    assert row["max_count"] == 2
    assert row["best_confidence"] == pytest.approx(0.9)
    assert row["labels"] == "bird"
    # Cover is the best photo (two birds outscores the single 0.9 bird? 0.6+0.05 < 0.9)
    cover = rig.db.get_photo(row["cover_photo_id"])
    assert cover["count"] == 1
    assert len(rig.sent) == 1 and rig.sent[0].count == 1


def test_no_photos_without_birds(rig):
    for _ in range(5):
        rig.frame([])
    assert rig.db.query("SELECT COUNT(*) AS n FROM photos")[0]["n"] == 0
    assert list(rig.media.root.rglob("*.jpg")) == []


def test_clip_recorded_and_linked(rig):
    rig.frame([bird()])
    assert rig.worker.clip is not None
    vid = rig.worker.visit.id
    for _ in range(9):
        rig.frame([bird()])
    assert rig.worker.clip is not None   # bird still there: keep recording
    for _ in range(5):
        rig.frame([])
    assert rig.worker.clip is None
    videos = rig.db.visit_videos(vid)
    assert len(videos) == 1 and videos[0]["size_bytes"] == 100


def test_clip_respects_max_length_and_cooldown(rig):
    rig.frame([bird()])
    for _ in range(31):
        rig.frame([bird()])
    assert rig.worker.clip is None  # stopped at max_clip_seconds
    rig.frame([], dt=25)            # visit ends
    rig.frame([bird()], dt=1)       # new visit within cooldown? last clip ended 26 s ago -> allowed
    assert rig.worker.clip is not None


def test_detector_skipped_without_motion(rig):
    rig.store.update({"capture": {"motion": {"enabled": True, "idle_detect_seconds": 30}}})
    rig.frame([])  # first frame primes the background and the idle timer
    calls = rig.det.calls
    for _ in range(10):
        rig.frame([])
    assert rig.det.calls == calls  # static picture: detector not run
    rig.frame([], dt=30)
    assert rig.det.calls == calls + 1  # idle check


def test_paused_does_not_detect(rig):
    rig.worker.paused = True
    rig.frame([bird()])
    assert rig.det.calls == 0 and rig.worker.visit is None


def test_manual_snapshot(rig):
    rig.worker.request_snapshot()
    rig.frame([])
    visits = rig.db.list_visits()
    assert len(visits) == 1 and "snapshot" in visits[0]["labels"]


def test_camera_failure_recovers(paths):
    store = ConfigStore(paths.config)
    db = Database(paths.db)
    bus = EventBus()
    attempts = []

    class Flaky(FakeBackend):
        def open(self):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("camera not detected")
            super().open()

    w = CameraWorker(store.settings.cameras[0], store=store, db=db,
                     media=MediaStore(paths.media, db, bus, store.settings.storage), detector=FakeDetector(),
                     notifier=Notifier(store.settings.notifications, paths.tmp), bus=bus,
                     backend_factory=lambda s: Flaky(s))
    w.start()
    deadline = time.time() + 6
    while time.time() < deadline and (len(attempts) < 2 or w.error):
        time.sleep(0.1)
    w.stop()
    assert len(attempts) >= 2 and w.error is None


def test_preroll_clip_starts_before_detection_and_waits_for_lag(paths):
    store = ConfigStore(paths.config)
    store.update({"capture": {"visit_gap": 30, "motion": {"enabled": False}},
                  "video": {"clip_seconds": 5, "max_clip_seconds": 60, "preroll_seconds": 5}})
    db = Database(paths.db)
    bus = EventBus()
    det = FakeDetector()
    clock = FakeClock()
    w = CameraWorker(store.settings.cameras[0], store=store, db=db,
                     media=MediaStore(paths.media, db, bus, store.settings.storage), detector=det,
                     notifier=Notifier(store.settings.notifications, paths.tmp), bus=bus,
                     backend_factory=lambda s: FakeBackend(s, clock), clock=clock)
    w._open()
    assert w.backend.preroll_active == 5 and w.status()["preroll"] == 5

    def frame(dets):
        clock.t += 1
        det.script.append(dets)
        w.step()

    frame([bird()])
    t_detect = clock.t
    assert w.clip.started_at == t_detect - 5
    for _ in range(5):
        frame([bird()])
    # Bird left; without pre-roll the clip would stop after 3 quiet seconds,
    # but the file lags by 5 s, so it must keep going for 3 + 5.
    for _ in range(7):
        frame([])
    assert w.clip is not None
    frame([])
    frame([])
    assert w.clip is None
    video = db.visit_videos(w.visit.id)[0]
    assert video["started_at"] == t_detect - 5


def test_preroll_off_when_video_disabled(paths):
    store = ConfigStore(paths.config)
    store.update({"video": {"enabled": False, "preroll_seconds": 5}})
    db = Database(paths.db)
    bus = EventBus()
    w = CameraWorker(store.settings.cameras[0], store=store, db=db,
                     media=MediaStore(paths.media, db, bus, store.settings.storage), detector=FakeDetector(),
                     notifier=Notifier(store.settings.notifications, paths.tmp), bus=bus,
                     backend_factory=lambda s: FakeBackend(s))
    w._open()
    assert w.backend.preroll_active == 0


def test_restart_needed_only_for_relevant_video_changes():
    from pirdfy.app import Pirdfy
    from pirdfy.config import VideoSettings

    need = Pirdfy._video_restart_needed
    off, on = VideoSettings(), VideoSettings(preroll_seconds=4)
    assert need(off, on) and need(on, off)
    assert not need(off, VideoSettings(bitrate=3_000_000))          # encoder starts per clip anyway
    assert need(on, VideoSettings(preroll_seconds=4, bitrate=3_000_000))
    assert not need(on, VideoSettings(preroll_seconds=4, cooldown=99))
    assert need(on, VideoSettings(preroll_seconds=4, enabled=False))


def test_mock_backend_writes_buffered_frames(tmp_path):
    import cv2

    from pirdfy.camera import MockBackend
    from pirdfy.config import CameraSettings

    b = MockBackend(CameraSettings(resolution=(640, 360)))
    b.configure_video(2, 1_000_000, 1)
    b.open()
    for _ in range(6):
        with b.capture():
            pass
    path = tmp_path / "clip.mp4"
    b.start_clip(path, 1_000_000)
    for _ in range(3):
        with b.capture():
            pass
    b.stop_clip()
    n = int(cv2.VideoCapture(str(path)).get(cv2.CAP_PROP_FRAME_COUNT))
    assert n == 9  # 6 buffered (all within 2 s) + 3 live
