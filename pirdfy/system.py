"""Host health: CPU, memory, temperature, throttling, version and updates."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import ROOT, __version__

log = logging.getLogger(__name__)

REPO = "andjar/pirdfy"

_THROTTLE_BITS = {
    0: "under-voltage", 1: "arm frequency capped", 2: "throttled", 3: "soft temperature limit",
}


def cpu_temperature() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def throttled() -> dict | None:
    """Decode `vcgencmd get_throttled` (Raspberry Pi only)."""
    if not shutil.which("vcgencmd"):
        return None
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
        value = int(out.strip().split("=")[1], 16)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None
    return {"now": [n for b, n in _THROTTLE_BITS.items() if value & (1 << b)],
            "since_boot": [n for b, n in _THROTTLE_BITS.items() if value & (1 << (b + 16))]}


class SystemMonitor:
    def __init__(self) -> None:
        self._cache: dict = {}
        self._cache_at = 0.0
        self._lock = threading.Lock()
        self._started = time.time()
        self._proc = None

    def snapshot(self) -> dict:
        with self._lock:
            if time.time() - self._cache_at < 5:
                return self._cache
            import psutil

            vm = psutil.virtual_memory()
            if self._proc is None:
                self._proc = psutil.Process()
                self._proc.cpu_percent(None)  # prime; first reading is meaningless
            proc = self._proc
            self._cache = {
                "cpu_percent": psutil.cpu_percent(interval=None),
                "load": [round(x, 2) for x in psutil.getloadavg()],
                "memory_percent": vm.percent,
                "process_mb": round(proc.memory_info().rss / 2 ** 20),
                # Percent of one core (can exceed 100 on the 4-core Pi 5).
                "process_cpu": round(proc.cpu_percent(None)),
                "temperature": cpu_temperature(),
                "throttled": throttled(),
                "uptime": int(time.time() - psutil.boot_time()),
                "service_uptime": int(time.time() - self._started),
            }
            self._cache_at = time.time()
            return self._cache


class Updater:
    """Checks GitHub for new versions and hands update requests to a root helper.

    The service runs unprivileged, so it only writes ``update/request``; the
    ``pirdfy-update.path`` systemd unit notices and runs ``install.sh update``.
    """

    def __init__(self, update_dir: Path, channel: str = "stable"):
        self.dir = Path(update_dir)
        self.channel = channel
        self._latest: dict | None = None
        self._checked_at = 0.0

    @staticmethod
    def current() -> dict:
        commit = None
        head = ROOT / ".git" / "HEAD"
        try:
            ref = head.read_text().strip()
            if ref.startswith("ref: "):
                ref_path = ROOT / ".git" / ref[5:]
                commit = ref_path.read_text().strip() if ref_path.exists() else None
            else:
                commit = ref
        except OSError:
            pass
        return {"version": __version__, "commit": commit[:7] if commit else None}

    def _fetch(self, url: str) -> dict | list:
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": f"pirdfy/{__version__}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.load(r)

    def check(self, force: bool = False) -> dict:
        if not force and self._latest and time.time() - self._checked_at < 6 * 3600:
            return self._latest
        cur = self.current()
        result: dict = {"current": cur, "channel": self.channel, "latest": None, "available": False,
                        "error": None, "checked_at": time.time()}
        try:
            if self.channel == "stable":
                tags = self._fetch(f"https://api.github.com/repos/{REPO}/tags?per_page=50")
                versions = sorted((_parse(t["name"]), t["name"]) for t in tags if _parse(t["name"]))
                if versions:
                    result["latest"] = versions[-1][1]
                    result["available"] = versions[-1][0] > (_parse(cur["version"]) or (0,))
            else:
                data = self._fetch(f"https://api.github.com/repos/{REPO}/commits/main")
                sha = data["sha"][:7]
                result["latest"] = sha
                result["available"] = bool(cur["commit"]) and sha != cur["commit"]
        except Exception as e:  # offline is normal
            result["error"] = f"Could not reach GitHub: {e}"
        self._latest, self._checked_at = result, time.time()
        return result

    def request_update(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "request").write_text(json.dumps({"channel": self.channel, "at": time.time()}))

    def status(self) -> dict | None:
        try:
            return json.loads((self.dir / "status.json").read_text())
        except (OSError, ValueError):
            return None

    @property
    def pending(self) -> bool:
        return (self.dir / "request").exists()


def _parse(tag: str) -> tuple[int, ...] | None:
    t = tag.lstrip("v")
    try:
        return tuple(int(x) for x in t.split("."))
    except ValueError:
        return None
