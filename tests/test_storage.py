import time
from collections import namedtuple

import numpy as np

from pirdfy import storage as storage_mod
from pirdfy.config import StorageSettings
from pirdfy.db import Database
from pirdfy.events import EventBus
from pirdfy.storage import MediaStore


def make(paths, **settings):
    db = Database(paths.db)
    return db, MediaStore(paths.media, db, EventBus(), StorageSettings(**settings))


def add_visit(db, media, ts, favorite=False):
    vid = db.start_visit(0, ts)
    info = media.save_photo(np.zeros((90, 160, 3), np.uint8), camera_id=0, ts=ts, quality=80)
    pid = db.add_photo(visit_id=vid, camera_id=0, taken_at=ts, detections=[("bird", 0.9, 0.1, 0.1, 0.2, 0.2)],
                       **info)
    db.set_cover(vid, pid)
    if favorite:
        db.set_favorite(vid, True)
    return vid, info


def test_retention_deletes_old_but_keeps_favorites(paths):
    db, media = make(paths, photo_retention_days=7, min_free_gb=0.2)
    now = time.time()
    old, old_info = add_visit(db, media, now - 10 * 86400)
    fav, _ = add_visit(db, media, now - 10 * 86400, favorite=True)
    new, _ = add_visit(db, media, now - 3600)
    media.housekeeping()
    assert db.get_visit(old) is None
    assert not (media.root / old_info["path"]).exists()
    assert not (media.root / old_info["thumb_path"]).exists()
    assert db.get_visit(fav) is not None and db.get_visit(new) is not None
    # cascade removed photo + detection rows
    assert db.query("SELECT COUNT(*) AS n FROM detections")[0]["n"] == 2


def test_space_guard_removes_oldest_first(paths, monkeypatch):
    db, media = make(paths, photo_retention_days=0, min_free_gb=5)
    now = time.time()
    ids = [add_visit(db, media, now - i * 60)[0] for i in (3, 2, 1)]
    Usage = namedtuple("Usage", "total used free")
    calls = {"n": 0}

    def fake_usage(_):
        calls["n"] += 1
        # Low on space until something has been deleted.
        free = 1 if db.get_visit(ids[0]) else 10
        return Usage(32 * 2**30, 0, free * 2**30)

    monkeypatch.setattr(storage_mod.shutil, "disk_usage", fake_usage)
    media.housekeeping()
    remaining = [v for v in ids if db.get_visit(v)]
    assert ids[0] not in remaining and remaining  # oldest gone, not everything


def test_resolve_rejects_traversal(paths):
    _, media = make(paths)
    try:
        media.resolve("../../etc/passwd")
    except ValueError:
        pass
    else:
        raise AssertionError("traversal allowed")


def test_db_migrations_idempotent(paths):
    Database(paths.db).close()
    db = Database(paths.db)
    assert db.schema_version == 1
    assert db.query("PRAGMA journal_mode")[0]["journal_mode"] == "wal"
    assert db.query("PRAGMA foreign_keys")[0]["foreign_keys"] == 1
