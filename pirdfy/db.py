"""SQLite storage with WAL, foreign keys and versioned migrations.

All timestamps are UNIX epoch seconds (UTC). Local-time grouping for statistics
is done in SQL with the 'localtime' modifier, so the Pi's time zone applies.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE visits (
        id              INTEGER PRIMARY KEY,
        camera_id       INTEGER NOT NULL,
        started_at      REAL NOT NULL,
        ended_at        REAL,
        max_count       INTEGER NOT NULL DEFAULT 0,
        best_confidence REAL NOT NULL DEFAULT 0,
        labels          TEXT NOT NULL DEFAULT '',
        cover_photo_id  INTEGER,
        favorite        INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_visits_started ON visits(started_at);
    CREATE TABLE photos (
        id         INTEGER PRIMARY KEY,
        visit_id   INTEGER REFERENCES visits(id) ON DELETE CASCADE,
        camera_id  INTEGER NOT NULL,
        taken_at   REAL NOT NULL,
        path       TEXT NOT NULL,
        thumb_path TEXT NOT NULL,
        width      INTEGER NOT NULL,
        height     INTEGER NOT NULL,
        count      INTEGER NOT NULL DEFAULT 0,
        size_bytes INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_photos_visit ON photos(visit_id);
    CREATE INDEX idx_photos_taken ON photos(taken_at);
    CREATE TABLE detections (
        id         INTEGER PRIMARY KEY,
        photo_id   INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
        label      TEXT NOT NULL,
        confidence REAL NOT NULL,
        x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL
    );
    CREATE INDEX idx_detections_photo ON detections(photo_id);
    CREATE TABLE videos (
        id         INTEGER PRIMARY KEY,
        visit_id   INTEGER REFERENCES visits(id) ON DELETE CASCADE,
        camera_id  INTEGER NOT NULL,
        started_at REAL NOT NULL,
        duration   REAL NOT NULL DEFAULT 0,
        path       TEXT NOT NULL,
        size_bytes INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_videos_visit ON videos(visit_id);
    CREATE INDEX idx_videos_started ON videos(started_at);
    """,
]


class Database:
    """One shared connection guarded by a lock.

    The workload (a few writes per second at most) is far below what a single
    SQLite connection handles; one connection keeps WAL checkpoints and
    transactions simple and avoids 'database is locked' errors between threads.
    """

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON",
                       "busy_timeout=5000", "temp_store=MEMORY"):
            self._conn.execute(f"PRAGMA {pragma}")
        self._migrate()

    # -- plumbing -----------------------------------------------------------------
    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
                self._conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version={i};\nCOMMIT;")

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- visits ---------------------------------------------------------------------
    def start_visit(self, camera_id: int, started_at: float) -> int:
        with self.tx() as c:
            return c.execute("INSERT INTO visits(camera_id, started_at, ended_at) VALUES (?,?,?)",
                             (camera_id, started_at, started_at)).lastrowid

    def touch_visit(self, visit_id: int, ended_at: float, count: int, confidence: float,
                    labels: Iterable[str]) -> None:
        with self.tx() as c:
            row = c.execute("SELECT labels FROM visits WHERE id=?", (visit_id,)).fetchone()
            if row is None:
                return
            merged = sorted(set(filter(None, row["labels"].split(","))) | set(labels))
            c.execute(
                """UPDATE visits SET ended_at=?, max_count=MAX(max_count, ?),
                       best_confidence=MAX(best_confidence, ?), labels=? WHERE id=?""",
                (ended_at, count, confidence, ",".join(merged), visit_id))

    def set_cover(self, visit_id: int, photo_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE visits SET cover_photo_id=? WHERE id=?", (photo_id, visit_id))

    def set_favorite(self, visit_id: int, favorite: bool) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE visits SET favorite=? WHERE id=?",
                             (int(favorite), visit_id)).rowcount > 0

    def get_visit(self, visit_id: int) -> dict | None:
        return self.one(f"{_VISIT_SELECT} WHERE v.id=?", (visit_id,))

    def list_visits(self, *, before_id: int | None = None, limit: int = 30, camera_id: int | None = None,
                    favorites: bool = False, day: str | None = None) -> list[dict]:
        where, params = ["v.cover_photo_id IS NOT NULL"], []
        if before_id is not None:
            where.append("v.id < ?")
            params.append(before_id)
        if camera_id is not None:
            where.append("v.camera_id = ?")
            params.append(camera_id)
        if favorites:
            where.append("v.favorite = 1")
        if day:
            where.append("date(v.started_at, 'unixepoch', 'localtime') = ?")
            params.append(day)
        params.append(limit)
        return self.query(f"{_VISIT_SELECT} WHERE {' AND '.join(where)} ORDER BY v.id DESC LIMIT ?", params)

    def visit_photos(self, visit_id: int) -> list[dict]:
        photos = self.query("SELECT * FROM photos WHERE visit_id=? ORDER BY taken_at", (visit_id,))
        if photos:
            ids = [p["id"] for p in photos]
            dets = self.query(
                f"SELECT * FROM detections WHERE photo_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids)
            by_photo: dict[int, list[dict]] = {}
            for d in dets:
                by_photo.setdefault(d["photo_id"], []).append(d)
            for p in photos:
                p["detections"] = by_photo.get(p["id"], [])
        return photos

    def visit_videos(self, visit_id: int) -> list[dict]:
        return self.query("SELECT * FROM videos WHERE visit_id=? ORDER BY started_at", (visit_id,))

    def visit_files(self, visit_id: int) -> list[str]:
        rows = self.query(
            """SELECT path AS p FROM photos WHERE visit_id=? UNION ALL
               SELECT thumb_path FROM photos WHERE visit_id=? UNION ALL
               SELECT path FROM videos WHERE visit_id=?""", (visit_id,) * 3)
        return [r["p"] for r in rows]

    def delete_visit(self, visit_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM visits WHERE id=?", (visit_id,))

    # -- photos / detections / videos -------------------------------------------------
    def add_photo(self, *, visit_id: int | None, camera_id: int, taken_at: float, path: str,
                  thumb_path: str, width: int, height: int, size_bytes: int,
                  detections: list[tuple[str, float, float, float, float, float]]) -> int:
        with self.tx() as c:
            pid = c.execute(
                """INSERT INTO photos(visit_id, camera_id, taken_at, path, thumb_path, width, height,
                                      count, size_bytes) VALUES (?,?,?,?,?,?,?,?,?)""",
                (visit_id, camera_id, taken_at, path, thumb_path, width, height, len(detections),
                 size_bytes)).lastrowid
            c.executemany(
                "INSERT INTO detections(photo_id, label, confidence, x, y, w, h) VALUES (?,?,?,?,?,?,?)",
                [(pid, *d) for d in detections])
            return pid

    def get_photo(self, photo_id: int) -> dict | None:
        return self.one("SELECT * FROM photos WHERE id=?", (photo_id,))

    def photo_count(self, visit_id: int) -> int:
        row = self.one("SELECT COUNT(*) AS n FROM photos WHERE visit_id=?", (visit_id,))
        return row["n"] if row else 0

    def add_video(self, *, visit_id: int | None, camera_id: int, started_at: float, duration: float,
                  path: str, size_bytes: int) -> int:
        with self.tx() as c:
            return c.execute(
                """INSERT INTO videos(visit_id, camera_id, started_at, duration, path, size_bytes)
                   VALUES (?,?,?,?,?,?)""",
                (visit_id, camera_id, started_at, duration, path, size_bytes)).lastrowid

    def get_video(self, video_id: int) -> dict | None:
        return self.one("SELECT * FROM videos WHERE id=?", (video_id,))

    def delete_video(self, video_id: int) -> None:
        with self.tx() as c:
            c.execute("DELETE FROM videos WHERE id=?", (video_id,))

    # -- retention ----------------------------------------------------------------------
    def visits_older_than(self, ts: float, limit: int = 200) -> list[int]:
        return [r["id"] for r in self.query(
            "SELECT id FROM visits WHERE favorite=0 AND started_at < ? ORDER BY id LIMIT ?", (ts, limit))]

    def oldest_visits(self, limit: int = 50) -> list[int]:
        return [r["id"] for r in self.query(
            "SELECT id FROM visits WHERE favorite=0 ORDER BY id LIMIT ?", (limit,))]

    def videos_older_than(self, ts: float, limit: int = 200) -> list[dict]:
        return self.query(
            """SELECT vid.* FROM videos vid LEFT JOIN visits v ON v.id = vid.visit_id
               WHERE (v.favorite IS NULL OR v.favorite = 0) AND vid.started_at < ?
               ORDER BY vid.id LIMIT ?""", (ts, limit))

    def media_bytes(self) -> int:
        row = self.one("""SELECT (SELECT COALESCE(SUM(size_bytes),0) FROM photos)
                               + (SELECT COALESCE(SUM(size_bytes),0) FROM videos) AS n""")
        return int(row["n"]) if row else 0

    def drop_empty_visits(self, older_than: float) -> int:
        """Visits that never got a photo (e.g. crash mid-visit)."""
        with self.tx() as c:
            return c.execute("DELETE FROM visits WHERE cover_photo_id IS NULL AND ended_at < ?",
                             (older_than,)).rowcount

    # -- statistics -------------------------------------------------------------------
    def stats_summary(self, now: float | None = None) -> dict:
        now = now or time.time()
        today = self.one(
            """SELECT COUNT(*) AS visits, COALESCE(MAX(max_count),0) AS peak,
                      COALESCE(SUM(ended_at - started_at),0) AS seconds
               FROM visits WHERE cover_photo_id IS NOT NULL
                 AND date(started_at,'unixepoch','localtime') = date(?,'unixepoch','localtime')""", (now,))
        total = self.one("SELECT COUNT(*) AS visits, MIN(started_at) AS first FROM visits "
                         "WHERE cover_photo_id IS NOT NULL")
        last = self.one("SELECT started_at FROM visits WHERE cover_photo_id IS NOT NULL "
                        "ORDER BY id DESC LIMIT 1")
        return {"today": today, "total_visits": total["visits"] if total else 0,
                "first_visit_at": total["first"] if total else None,
                "last_visit_at": last["started_at"] if last else None}

    def stats_daily(self, days: int) -> list[dict]:
        return self.query(
            """SELECT date(started_at,'unixepoch','localtime') AS day, COUNT(*) AS visits,
                      MAX(max_count) AS peak, CAST(SUM(ended_at - started_at) AS INTEGER) AS seconds
               FROM visits WHERE cover_photo_id IS NOT NULL
                 AND started_at >= CAST(strftime('%s', date('now','localtime','-' || ? || ' days'), 'utc') AS REAL)
               GROUP BY day ORDER BY day""", (days - 1,))

    def stats_heatmap(self, days: int) -> list[dict]:
        return self.query(
            """SELECT date(started_at,'unixepoch','localtime') AS day,
                      CAST(strftime('%H', started_at,'unixepoch','localtime') AS INTEGER) AS hour,
                      COUNT(*) AS visits
               FROM visits WHERE cover_photo_id IS NOT NULL
                 AND started_at >= CAST(strftime('%s', date('now','localtime','-' || ? || ' days'), 'utc') AS REAL)
               GROUP BY day, hour ORDER BY day, hour""", (days - 1,))


_VISIT_SELECT = """
SELECT v.*, p.thumb_path AS cover_thumb, p.width AS cover_width, p.height AS cover_height,
       (SELECT COUNT(*) FROM photos WHERE visit_id = v.id) AS photo_count,
       (SELECT id FROM videos WHERE visit_id = v.id ORDER BY id LIMIT 1) AS video_id
FROM visits v LEFT JOIN photos p ON p.id = v.cover_photo_id
"""
