"""On-device benchmark: what does pre-roll (always-on video encoding) cost?

Run on the Pi with the service stopped (the camera can only be opened once):

    sudo pirdfy bench                # uses your camera settings, 60 s per phase
    sudo pirdfy bench --seconds 120 --preroll 5

It measures Pirdfy's CPU use, system CPU, temperature and throttling in four
phases, records a short test clip with pre-roll, and prints a recommendation.
Temperature keeps rising for several minutes, so longer phases are more honest.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .camera import CameraBackend, make_backend
from .config import ConfigStore
from .detector import Detector
from .motion import MotionGate
from .paths import Paths
from .system import cpu_temperature, throttled


@dataclass
class Result:
    name: str
    seconds: float
    frames: int
    process_cpu: float     # % of one core
    system_cpu: float      # % of the whole CPU (all cores)
    temp_start: float | None
    temp_end: float | None
    detector_ms: float | None
    throttled: list[str]

    @property
    def fps(self) -> float:
        return self.frames / self.seconds if self.seconds else 0.0


def run_phase(name: str, backend: CameraBackend, seconds: float, interval: float, gate: MotionGate,
              detector: Detector | None) -> Result:
    import psutil

    proc = psutil.Process()
    psutil.cpu_percent(None)
    proc_t0 = sum(proc.cpu_times()[:2])
    t0 = time.monotonic()
    temp0 = cpu_temperature()
    frames, det_ms = 0, []
    while time.monotonic() - t0 < seconds:
        tick = time.monotonic()
        with backend.capture() as f:
            gate.score(f.gray())
            if detector is not None and detector.ready:
                d0 = time.perf_counter()
                detector.detect(f.lores_rgb())
                det_ms.append((time.perf_counter() - d0) * 1000)
        frames += 1
        time.sleep(max(0.0, interval - (time.monotonic() - tick)))
    wall = time.monotonic() - t0
    thr = throttled() or {}
    return Result(name=name, seconds=wall, frames=frames,
                  process_cpu=100 * (sum(proc.cpu_times()[:2]) - proc_t0) / wall,
                  system_cpu=psutil.cpu_percent(None), temp_start=temp0, temp_end=cpu_temperature(),
                  detector_ms=sum(det_ms) / len(det_ms) if det_ms else None,
                  throttled=thr.get("now", []))


def test_clip(backend: CameraBackend, folder: Path, preroll: int, bitrate: int) -> tuple[bool, str]:
    """Record a short clip with pre-roll and check it is longer than the live part."""
    path = folder / "bench-preroll.mp4"
    live = 3.0
    backend.start_clip(path, bitrate)
    t0 = time.monotonic()
    while time.monotonic() - t0 < live + preroll + 0.5:  # file lags by the pre-roll
        with backend.capture():
            pass
    backend.stop_clip()
    if not path.exists() or path.stat().st_size == 0:
        return False, "no video was written"
    duration = None
    with contextlib.suppress(Exception):
        import av

        # Fragmented MP4 has no total duration in the header; measure the packet timestamps.
        with av.open(str(path)) as c:
            stream = c.streams.video[0]
            pts = [p.pts for p in c.demux(stream) if p.pts is not None]
            if len(pts) > 1:
                duration = float((max(pts) - min(pts)) * stream.time_base)
    size = path.stat().st_size / 1e6
    if duration is None:
        return True, f"{size:.1f} MB written (could not read its length)"
    ok = live + preroll * 0.6 <= duration <= live + preroll + 3
    return ok, f"{duration:.1f} s long, {size:.1f} MB (expected about {live + preroll:.0f} s)"


def fmt_temp(r: Result) -> str:
    if r.temp_end is None:
        return "n/a"
    start = f"{r.temp_start:.0f}→" if r.temp_start is not None else ""
    return f"{start}{r.temp_end:.0f} °C"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pirdfy bench", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=60, help="length of each phase (default 60)")
    ap.add_argument("--preroll", type=int, default=None, help="pre-roll seconds to test (default: setting or 5)")
    ap.add_argument("--camera", type=int, default=None, help="camera id (default: first enabled)")
    args = ap.parse_args(argv)

    paths = Paths.from_env()
    settings = ConfigStore(paths.config).settings
    cams = [c for c in settings.cameras if c.enabled] or settings.cameras
    cam = next((c for c in cams if c.id == args.camera), cams[0])
    preroll = args.preroll if args.preroll is not None else (settings.video.preroll_seconds or 5)
    vs = settings.video
    interval = settings.capture.interval

    print(f"Pirdfy bench: camera {cam.id} ({cam.name}), {cam.resolution[0]}x{cam.resolution[1]} @ {cam.fps} fps, "
          f"analysis every {interval} s, pre-roll {preroll} s, {args.seconds:.0f} s per phase\n")
    detector = Detector(paths.models, settings.detection)
    if not detector.load():
        print(f"  (detector unavailable: {detector.error}; detector phases will only measure the camera)")

    results: list[Result] = []
    clip_check: tuple[bool, str] | None = None

    def phases(preroll_s: int, label: str) -> None:
        nonlocal clip_check
        backend = make_backend(cam)
        backend.configure_video(preroll_s, vs.bitrate, vs.preroll_threads)
        try:
            backend.open()
        except Exception as e:
            raise SystemExit(f"Could not open the camera: {e}\nIs the Pirdfy service still running? "
                             "Use 'sudo pirdfy bench', which stops it for you.") from e
        try:
            if preroll_s and not backend.preroll_active:
                raise SystemExit(f"Pre-roll could not start: {backend.preroll_error}")
            time.sleep(2)  # let exposure and the encoder settle
            gate = MotionGate(settings.capture.motion)
            for with_det in (False, True):
                name = f"{label}{' + detector every frame' if with_det else ''}"
                print(f"  running: {name} ...", flush=True)
                results.append(run_phase(name, backend, args.seconds, interval, gate,
                                         detector if with_det else None))
            if preroll_s:
                tmp = Path(tempfile.mkdtemp(prefix="pirdfy-bench-", dir=paths.tmp))
                try:
                    clip_check = test_clip(backend, tmp, preroll_s, vs.bitrate)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
        finally:
            backend.close()

    phases(0, "Camera, idle")
    phases(preroll, f"Camera + {preroll} s pre-roll")

    print()
    head = f"{'Phase':44} {'Pirdfy CPU':>11} {'All CPUs':>9} {'Temp':>12} {'Detector':>9} {'fps':>5}"
    print(head)
    print("-" * len(head))
    for r in results:
        det = f"{r.detector_ms:.0f} ms" if r.detector_ms else "-"
        print(f"{r.name:44} {r.process_cpu:>9.0f} % {r.system_cpu:>7.0f} % {fmt_temp(r):>12} {det:>9} {r.fps:>5.1f}")
        if r.throttled:
            print(f"   ! {', '.join(r.throttled)}")

    base, base_det, pre, pre_det = results
    extra = pre.process_cpu - base.process_cpu
    print(f"\nPre-roll costs about {extra:.0f} % of one core all day "
          f"({extra / 4:.0f} % of the whole Pi 5), even when no bird is there.")
    if pre_det.detector_ms and base_det.detector_ms:
        slow = pre_det.detector_ms / base_det.detector_ms
        print(f"Detection while pre-roll runs: {pre_det.detector_ms:.0f} ms vs {base_det.detector_ms:.0f} ms "
              f"({slow:.1f}x).")
    if clip_check:
        print(f"Test clip with pre-roll: {'OK' if clip_check[0] else 'PROBLEM'}, {clip_check[1]}.")

    hot = any(r.temp_end and r.temp_end >= 80 for r in results)
    throttling = any(r.throttled for r in results)
    print("\nRecommendation:", end=" ")
    if throttling or hot:
        print("not with this setup. The Pi got too hot or was throttled; add a cooler (Active Cooler) or a "
              "better power supply, or lower the frame rate or resolution, then run the bench again.")
    elif extra > 150 or (pre_det.detector_ms and base_det.detector_ms and pre_det.detector_ms > 2 * base_det.detector_ms):
        print("possible, but heavy. Try a lower frame rate (e.g. 15 fps) or 1280x720 and run the bench again.")
    else:
        print(f"fine. Turn it on in Settings > Video (or set video.preroll_seconds: {preroll}).")
    if clip_check and not clip_check[0]:
        print("Note: the test clip looked wrong; please report this with 'pirdfy logs -n 200'.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
