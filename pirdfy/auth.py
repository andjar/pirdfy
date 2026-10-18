"""Single-user password auth with signed, expiring session cookies.

* Password stored as scrypt hash (stdlib) in ``auth.json`` (mode 0600).
* Session cookie = base64(json payload) + HMAC-SHA256; changing the password
  bumps ``generation`` and invalidates every existing session.
* Login attempts are rate limited per client address.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

COOKIE = "pirdfy_session"
MIN_PASSWORD = 8
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return "scrypt${n}${r}${p}${salt}${hash}".format(
        **_SCRYPT, salt=base64.b64encode(salt).decode(), hash=base64.b64encode(digest).decode())


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, digest = encoded.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                              dklen=32)
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class AuthStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = self._load()
        self._failures: dict[str, tuple[int, float]] = {}

    def _load(self) -> dict:
        data: dict = {}
        if self.path.exists():
            data = json.loads(self.path.read_text())
        changed = False
        if "secret" not in data:
            data["secret"] = secrets.token_hex(32)
            changed = True
        data.setdefault("generation", 1)
        if changed:
            self._write(data)
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    @property
    def has_password(self) -> bool:
        return bool(self._data.get("password"))

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"Password must be at least {MIN_PASSWORD} characters")
        with self._lock:
            self._data["password"] = hash_password(password)
            self._data["generation"] = int(self._data.get("generation", 1)) + 1
            self._write(self._data)

    def check_password(self, password: str) -> bool:
        encoded = self._data.get("password")
        return bool(encoded) and verify_password(password, encoded)

    # -- sessions ---------------------------------------------------------------------
    def _sign(self, payload: bytes) -> str:
        return _b64(hmac.new(bytes.fromhex(self._data["secret"]), payload, hashlib.sha256).digest())

    def issue(self, days: int) -> str:
        payload = json.dumps({"exp": int(time.time() + days * 86400), "g": self._data["generation"]},
                             separators=(",", ":")).encode()
        return f"{_b64(payload)}.{self._sign(payload)}"

    def valid(self, token: str | None) -> bool:
        if not token or "." not in token:
            return False
        body, sig = token.rsplit(".", 1)
        try:
            payload = _unb64(body)
        except ValueError:
            return False
        if not hmac.compare_digest(sig, self._sign(payload)):
            return False
        try:
            data = json.loads(payload)
        except ValueError:
            return False
        return data.get("exp", 0) > time.time() and data.get("g") == self._data["generation"]

    # -- rate limiting --------------------------------------------------------------------
    def retry_after(self, client: str) -> float:
        count, last = self._failures.get(client, (0, 0.0))
        if count < 5:
            return 0.0
        wait = min(2 ** (count - 5), 300)
        return max(0.0, last + wait - time.time())

    def record_failure(self, client: str) -> None:
        count, _ = self._failures.get(client, (0, 0.0))
        self._failures[client] = (count + 1, time.time())
        if len(self._failures) > 1000:  # bound memory
            self._failures.pop(next(iter(self._failures)))

    def record_success(self, client: str) -> None:
        self._failures.pop(client, None)
