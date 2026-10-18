"""Filesystem layout.

Installed (systemd sets the env vars):
    /opt/pirdfy              code + venv (read-only to the service)
    /etc/pirdfy/config.yaml  user overrides
    /var/lib/pirdfy          database, media, models, secrets

Development: everything defaults to ./dev-data and ./dev-data/config.yaml.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Paths:
    data: Path
    config: Path

    @property
    def db(self) -> Path:
        return self.data / "pirdfy.db"

    @property
    def media(self) -> Path:
        return self.data / "media"

    @property
    def models(self) -> Path:
        return self.data / "models"

    @property
    def tmp(self) -> Path:
        return self.data / "tmp"

    @property
    def auth(self) -> Path:
        return self.data / "auth.json"

    @property
    def update_dir(self) -> Path:
        return self.data / "update"

    def ensure(self) -> None:
        for d in (self.data, self.media, self.models, self.tmp, self.update_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Paths:
        data = Path(os.environ.get("PIRDFY_DATA_DIR", "dev-data")).resolve()
        config = Path(os.environ.get("PIRDFY_CONFIG", str(data / "config.yaml"))).resolve()
        return cls(data=data, config=config)
