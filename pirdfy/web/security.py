"""ASGI middleware: authentication, same-origin checks and security headers."""

from __future__ import annotations

import ipaddress
import json
from http.cookies import SimpleCookie
from urllib.parse import quote, urlsplit

from ..auth import COOKIE, AuthStore

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = {"/login", "/setup", "/api/auth/login", "/api/auth/setup", "/healthz", "/favicon.svg",
                "/manifest.webmanifest"}

CSP = ("default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; style-src 'self'; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def is_private(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _headers(scope) -> dict[str, str]:
    return {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}


def session_token(scope) -> str | None:
    raw = _headers(scope).get("cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel else None


class SecurityMiddleware:
    def __init__(self, app, auth: AuthStore):
        self.app = app
        self.auth = auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path: str = scope["path"]
        method: str = scope["method"]
        headers = _headers(scope)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = list(message.get("headers", []))
                h += [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"),
                      (b"x-frame-options", b"DENY"), (b"content-security-policy", CSP.encode())]
                message = {**message, "headers": h}
            await send(message)

        # Cross-site request forgery: browsers always send Origin on cross-site unsafe requests.
        if method not in SAFE_METHODS:
            origin = headers.get("origin")
            if origin and urlsplit(origin).netloc != headers.get("host"):
                return await _json(send_with_headers, 403, {"detail": "Cross-origin request refused"})

        public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
        if public or self.auth.valid(session_token(scope)):
            return await self.app(scope, receive, send_with_headers)

        if path.startswith("/api/") or path.startswith("/media/"):
            return await _json(send_with_headers, 401, {"detail": "Not logged in"})
        target = "/setup" if not self.auth.has_password else "/login"
        nxt = path + (("?" + scope["query_string"].decode()) if scope.get("query_string") else "")
        location = f"{target}?next={quote(nxt)}" if path != "/" else target
        await send_with_headers({"type": "http.response.start", "status": 303,
                                 "headers": [(b"location", location.encode()), (b"cache-control", b"no-store")]})
        await send_with_headers({"type": "http.response.body", "body": b""})


async def _json(send, status: int, body: dict) -> None:
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())]})
    await send({"type": "http.response.body", "body": data})
