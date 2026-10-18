"""Pirdfy: bird feeder camera detector for Raspberry Pi 5."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read_version() -> str:
    try:
        return (ROOT / "VERSION").read_text().strip()
    except OSError:
        return "0.0.0"


__version__ = _read_version()
