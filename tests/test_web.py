import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from pirdfy.app import Pirdfy
from pirdfy.auth import AuthStore, hash_password, verify_password
from pirdfy.web.app import create_app

from .conftest import FakeBackend, FakeDetector

PW = "correct-horse"


@pytest.fixture
def pirdfy(paths):
    p = Pirdfy(paths, detector=FakeDetector(), backend_factory=lambda s: FakeBackend(s))
    p.auth.set_password(PW)
    yield p
    p.stop()


@pytest.fixture
def client(pirdfy):
    with TestClient(create_app(pirdfy, manage_lifecycle=False), base_url="http://pirdfy.local") as c:
        yield c


def login(c):
    r = c.post("/api/auth/login", json={"password": PW})
    assert r.status_code == 200, r.text


def test_password_hashing():
    h = hash_password("hunter22")
    assert verify_password("hunter22", h) and not verify_password("hunter23", h)


def test_session_invalidated_by_password_change(tmp_path):
    a = AuthStore(tmp_path / "auth.json")
    a.set_password("first-pass")
    token = a.issue(1)
    assert a.valid(token)
    a.set_password("second-pass")
    assert not a.valid(token)
    assert not a.valid(token[:-2] + "xx")


def test_pages_redirect_and_api_401(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/gallery", follow_redirects=False).headers["location"] == "/login?next=/gallery"
    assert client.get("/api/status").status_code == 401
    assert client.get("/media/photos/1.jpg").status_code == 401
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/login").status_code == 200


def test_login_and_pages(client):
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    login(client)
    for page in ("/", "/gallery", "/stats", "/settings"):
        r = client.get(page)
        assert r.status_code == 200 and "Pirdfy" in r.text
        assert "content-security-policy" in r.headers
    s = client.get("/api/status").json()
    assert s["detector"]["ready"] is True and "disk" in s


def test_login_rate_limited(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"password": "bad"})
    r = client.post("/api/auth/login", json={"password": PW})
    assert r.status_code == 429


def test_cross_origin_post_refused(client):
    login(client)
    r = client.post("/api/capture/pause", headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.post("/api/capture/pause", headers={"origin": "http://pirdfy.local"}).status_code == 200


def test_settings_patch_validation_and_masking(client, pirdfy):
    login(client)
    r = client.patch("/api/settings", json={"capture": {"interval": 0.01}})
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "capture.interval"
    r = client.patch("/api/settings", json={"notifications": {"urls": ["tgram://tok/1"]}, "video": {"cooldown": 5}})
    assert r.status_code == 200
    assert "tok" not in r.text
    assert pirdfy.store.settings.video.cooldown == 5
    assert pirdfy.paths.config.exists()


def test_visits_api_and_media(client, pirdfy):
    login(client)
    db, media = pirdfy.db, pirdfy.media
    ids = []
    for i in range(3):
        ts = time.time() - 100 + i
        vid = db.start_visit(0, ts)
        info = media.save_photo(np.zeros((90, 160, 3), np.uint8), camera_id=0, ts=ts, quality=80)
        pid = db.add_photo(visit_id=vid, camera_id=0, taken_at=ts, detections=[("bird", 0.8, .1, .1, .2, .2)], **info)
        db.set_cover(vid, pid)
        db.touch_visit(vid, ts + 5, 1, 0.8, {"bird"})
        ids.append((vid, pid))
    page = client.get("/api/visits?limit=2").json()
    assert [v["id"] for v in page["visits"]] == [ids[2][0], ids[1][0]] and page["next"] == ids[1][0]
    page2 = client.get(f"/api/visits?limit=2&before={page['next']}").json()
    assert [v["id"] for v in page2["visits"]] == [ids[0][0]] and page2["next"] is None
    detail = client.get(f"/api/visits/{ids[0][0]}").json()
    assert detail["photos"][0]["detections"][0]["label"] == "bird"
    assert "path" not in detail["photos"][0]
    img = client.get(f"/media/photos/{ids[0][1]}.jpg?thumb=1")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.put(f"/api/visits/{ids[0][0]}/favorite", json={"favorite": True}).status_code == 200
    assert client.get("/api/visits?favorites=true").json()["visits"][0]["id"] == ids[0][0]
    assert client.delete(f"/api/visits/{ids[1][0]}").status_code == 200
    assert client.get(f"/media/photos/{ids[1][1]}.jpg").status_code == 404
    stats = client.get("/api/stats?days=7").json()
    assert stats["summary"]["today"]["visits"] >= 1


def test_setup_only_without_password_and_from_lan(paths):
    p = Pirdfy(paths, detector=FakeDetector(), backend_factory=lambda s: FakeBackend(s))
    try:
        with TestClient(create_app(p, manage_lifecycle=False), client=("8.8.8.8", 5000)) as c:
            assert c.get("/", follow_redirects=False).headers["location"] == "/setup"
            assert c.post("/api/auth/setup", json={"password": "longenough"}).status_code == 403
        with TestClient(create_app(p, manage_lifecycle=False), client=("192.168.1.20", 5000)) as c:
            assert c.post("/api/auth/setup", json={"password": "short"}).status_code == 422
            assert c.post("/api/auth/setup", json={"password": "longenough"}).status_code == 200
            assert c.get("/api/status").status_code == 200  # logged in by setup
            assert c.post("/api/auth/setup", json={"password": "another-one"}).status_code == 409
    finally:
        p.stop()
