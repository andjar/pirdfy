"""HTTP API and pages (FastAPI). Live updates use Server-Sent Events."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Body, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from .. import __version__
from ..app import Pirdfy
from ..auth import COOKIE, MIN_PASSWORD
from ..config import AWB_MODES, COCO_CLASSES
from ..storage import encode_jpeg
from .security import SecurityMiddleware, is_private

log = logging.getLogger(__name__)
HERE = Path(__file__).parent


class PasswordIn(BaseModel):
    password: Annotated[str, Field(min_length=1, max_length=200)]


class ChangePasswordIn(BaseModel):
    current: Annotated[str, Field(max_length=200)]
    new: Annotated[str, Field(min_length=MIN_PASSWORD, max_length=200)]


class FavoriteIn(BaseModel):
    favorite: bool


def create_app(p: Pirdfy, *, manage_lifecycle: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        p.bus.attach_loop(asyncio.get_running_loop())
        if manage_lifecycle:
            await asyncio.to_thread(p.start)
        try:
            yield
        finally:
            if manage_lifecycle:
                await asyncio.to_thread(p.stop)

    app = FastAPI(title="Pirdfy", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(SecurityMiddleware, auth=p.auth)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(version=__version__)

    def cookie_secure(request: Request) -> bool:
        return request.url.scheme == "https"

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def worker(camera_id: int):
        w = p.workers.get(camera_id)
        if w is None:
            raise HTTPException(404, "Camera not found or disabled")
        return w

    # -- pages --------------------------------------------------------------------------
    def page(name: str, request: Request, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, f"{name}.html", {"page": name, **ctx})

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def live_page(request: Request):
        return page("live", request)

    @app.get("/gallery", response_class=HTMLResponse, include_in_schema=False)
    def gallery_page(request: Request):
        return page("gallery", request)

    @app.get("/stats", response_class=HTMLResponse, include_in_schema=False)
    def stats_page(request: Request):
        return page("stats", request)

    @app.get("/settings", response_class=HTMLResponse, include_in_schema=False)
    def settings_page(request: Request):
        return page("settings", request, classes=COCO_CLASSES, awb_modes=AWB_MODES)

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request):
        if not p.auth.has_password:
            return RedirectResponse("/setup", 303)
        return page("login", request, setup=False)

    @app.get("/setup", response_class=HTMLResponse, include_in_schema=False)
    def setup_page(request: Request):
        if p.auth.has_password:
            return RedirectResponse("/login", 303)
        return page("login", request, setup=True, allowed=is_private(client_ip(request)))

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        return FileResponse(HERE / "static" / "favicon.svg", headers={"cache-control": "max-age=604800"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(HERE / "static" / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        cams_ok = all(w.error is None for w in p.workers.values())
        return {"status": "ok", "version": __version__, "cameras_ok": cams_ok, "detector": p.detector.ready}

    # -- auth ---------------------------------------------------------------------------
    def _login_response(request: Request) -> JSONResponse:
        days = p.store.settings.web.session_days
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, p.auth.issue(days), max_age=days * 86400, httponly=True, samesite="lax",
                        secure=cookie_secure(request), path="/")
        return resp

    @app.post("/api/auth/login", tags=["auth"])
    def login(body: PasswordIn, request: Request):
        ip = client_ip(request)
        wait = p.auth.retry_after(ip)
        if wait > 0:
            raise HTTPException(429, f"Too many attempts. Try again in {int(wait) + 1} s.",
                                headers={"retry-after": str(int(wait) + 1)})
        if not p.auth.check_password(body.password):
            p.auth.record_failure(ip)
            log.warning("Failed login from %s", ip)
            raise HTTPException(401, "Wrong password")
        p.auth.record_success(ip)
        return _login_response(request)

    @app.post("/api/auth/setup", tags=["auth"])
    def setup(body: PasswordIn, request: Request):
        if p.auth.has_password:
            raise HTTPException(409, "A password is already set")
        if not is_private(client_ip(request)):
            raise HTTPException(403, "Initial setup is only allowed from the local network")
        try:
            p.auth.set_password(body.password)
        except ValueError as e:
            raise HTTPException(422, str(e)) from None
        return _login_response(request)

    @app.post("/api/auth/logout", tags=["auth"])
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.post("/api/auth/password", tags=["auth"])
    def change_password(body: ChangePasswordIn, request: Request):
        if not p.auth.check_password(body.current):
            raise HTTPException(401, "Current password is wrong")
        p.auth.set_password(body.new)
        return _login_response(request)  # old sessions are now invalid; keep this one

    # -- live status ----------------------------------------------------------------------
    @app.get("/api/status", tags=["status"])
    def status():
        return {
            "version": __version__,
            "cameras": [w.status() for w in p.workers.values()],
            "detector": p.detector.info(),
            "disk": p.media.disk(),
            "system": p.monitor.snapshot(),
            "warnings": p.warnings(),
            "summary": p.db.stats_summary(),
        }

    @app.get("/api/events", tags=["status"])
    async def events(request: Request):
        q = p.bus.subscribe()

        async def stream():
            try:
                yield "retry: 5000\nevent: hello\ndata: {}\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=20)
                        yield msg
                    except TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": ping\n\n"
            finally:
                p.bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-store", "x-accel-buffering": "no"})

    @app.get("/api/cameras/{camera_id}/snapshot.jpg", tags=["cameras"])
    def snapshot(camera_id: int):
        w = worker(camera_id)
        frame = w.latest_frame()
        if frame is None:
            raise HTTPException(503, "No frame yet")
        # Boxes travel with the frame so the overlay always matches the picture.
        dets = json.dumps([d.__dict__ for d in w.last_detections], separators=(",", ":"))
        return Response(encode_jpeg(frame, 75), media_type="image/jpeg",
                        headers={"cache-control": "no-store", "x-detections": dets})

    @app.post("/api/cameras/{camera_id}/snapshot", tags=["cameras"])
    def save_snapshot(camera_id: int):
        worker(camera_id).request_snapshot()
        return {"ok": True}

    @app.post("/api/capture/{action}", tags=["cameras"])
    def capture_control(action: str):
        if action not in ("pause", "resume"):
            raise HTTPException(404)
        for w in p.workers.values():
            w.paused = action == "pause"
        p.bus.publish("capture", {"paused": action == "pause"})
        return {"paused": action == "pause"}

    # -- visits & media ---------------------------------------------------------------------
    @app.get("/api/visits", tags=["visits"])
    def list_visits(before: int | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 30,
                    camera: int | None = None, favorites: bool = False,
                    day: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")] = None):
        rows = p.db.list_visits(before_id=before, limit=limit, camera_id=camera, favorites=favorites, day=day)
        for r in rows:
            r["labels"] = [x for x in r["labels"].split(",") if x]
            r.pop("cover_thumb", None)
        return {"visits": rows, "next": rows[-1]["id"] if len(rows) == limit else None}

    @app.get("/api/visits/{visit_id}", tags=["visits"])
    def get_visit(visit_id: int):
        v = p.db.get_visit(visit_id)
        if v is None:
            raise HTTPException(404, "Visit not found")
        v["labels"] = [x for x in v["labels"].split(",") if x]
        v.pop("cover_thumb", None)
        photos = p.db.visit_photos(visit_id)
        for ph in photos:
            ph.pop("path"), ph.pop("thumb_path")
        videos = p.db.visit_videos(visit_id)
        for vid in videos:
            vid.pop("path")
        return {"visit": v, "photos": photos, "videos": videos}

    @app.put("/api/visits/{visit_id}/favorite", tags=["visits"])
    def favorite(visit_id: int, body: FavoriteIn):
        if not p.db.set_favorite(visit_id, body.favorite):
            raise HTTPException(404, "Visit not found")
        return {"ok": True}

    @app.delete("/api/visits/{visit_id}", tags=["visits"])
    def delete_visit(visit_id: int):
        if p.db.get_visit(visit_id) is None:
            raise HTTPException(404, "Visit not found")
        if any(w.visit and w.visit.id == visit_id for w in p.workers.values()):
            raise HTTPException(409, "Visit is still in progress")
        p.media.delete_visit(visit_id)
        return {"ok": True}

    def _media_file(rel: str, media_type: str, filename: str | None = None) -> FileResponse:
        try:
            path = p.media.resolve(rel)
        except ValueError:
            raise HTTPException(404) from None
        if not path.exists():
            raise HTTPException(404, "File missing")
        headers = {"cache-control": "private, max-age=31536000, immutable"}
        return FileResponse(path, media_type=media_type, headers=headers, filename=filename,
                            content_disposition_type="inline")

    @app.get("/media/photos/{photo_id}.jpg", tags=["media"])
    def photo_file(photo_id: int, thumb: bool = False, download: bool = False):
        ph = p.db.get_photo(photo_id)
        if ph is None:
            raise HTTPException(404)
        name = f"pirdfy-{photo_id}.jpg" if download else None
        return _media_file(ph["thumb_path"] if thumb else ph["path"], "image/jpeg", name)

    @app.get("/media/videos/{video_id}.mp4", tags=["media"])
    def video_file(video_id: int):
        v = p.db.get_video(video_id)
        if v is None:
            raise HTTPException(404)
        return _media_file(v["path"], "video/mp4")

    # -- statistics ---------------------------------------------------------------------------
    @app.get("/api/stats", tags=["stats"])
    def stats(days: Annotated[int, Query(ge=1, le=366)] = 14):
        return {"days": days, "summary": p.db.stats_summary(), "daily": p.db.stats_daily(days),
                "heatmap": p.db.stats_heatmap(days)}

    # -- settings ---------------------------------------------------------------------------
    @app.get("/api/settings", tags=["settings"])
    def get_settings():
        return p.store.to_public_dict()

    @app.patch("/api/settings", tags=["settings"])
    def patch_settings(patch: Annotated[dict, Body()]):
        try:
            p.store.update(patch)
        except ValidationError as e:
            errors = [{"field": ".".join(str(x) for x in err["loc"]), "message": err["msg"]}
                      for err in e.errors()]
            return JSONResponse({"detail": "Invalid settings", "errors": errors}, status_code=422)
        return p.store.to_public_dict()

    @app.post("/api/notifications/test", tags=["settings"])
    def test_notification():
        ok, err = p.notifier.send_test()
        if not ok:
            raise HTTPException(502, err or "Failed")
        return {"ok": True}

    # -- system ---------------------------------------------------------------------------
    @app.get("/api/system/version", tags=["system"])
    def version(force: bool = False):
        if not p.store.settings.updates.check and not force:
            return {"current": p.updater.current(), "latest": None, "available": False, "error": None}
        return p.updater.check(force=force)

    @app.get("/api/system/update", tags=["system"])
    def update_status():
        return {"pending": p.updater.pending, "status": p.updater.status()}

    @app.post("/api/system/update", tags=["system"])
    def update():
        p.updater.request_update()
        return {"ok": True}

    @app.post("/api/system/restart", tags=["system"])
    def restart():
        # systemd (Restart=always) brings the service back up.
        threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return {"ok": True}

    return app
