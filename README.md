# Pirdfy

A bird feeder camera for the **Raspberry Pi 5**. It watches the feeder, notices when a
bird arrives, saves a few good photos and a short video of each visit, and shows
everything on a dashboard you can open from your phone. It also works offline.

- **Visits, not thousands of frames.** Photos are only written to the SD card while a bird is
  there (at most one every few seconds, plus one whenever more birds arrive), grouped into visits.
- **Light on the Pi.** A cheap motion check wakes the detector only when the picture changes; the
  detector (YOLO, ONNX Runtime) runs on a small 640 px stream from the camera's ISP. No PyTorch.
- **Video that plays everywhere.** H.264 MP4 clips, recorded without interrupting detection,
  extended while the bird stays.
- **Push notifications** to ntfy, Telegram, Pushover, Discord, email and
  [many more](https://github.com/caronc/apprise/wiki), with a close-up photo.
- **Keeps itself tidy.** Retention per photo/video age, a storage cap and a free-space guard;
  favourites are kept forever.
- **Password protected**, hardened systemd service, no CDN or web fonts.
- **One command to install, one button to update**, with automatic rollback if an update fails.

## Install

Raspberry Pi OS (64-bit) Bookworm or Trixie on a Raspberry Pi 5 with a Camera Module (v2, v3 or HQ):

```bash
curl -fsSL https://raw.githubusercontent.com/andjar/pirdfy/main/install.sh | sudo bash
```

The installer asks for a dashboard password (or generates one) and prints the address, usually
`http://<hostname>.local:8080`. Running it again is safe: it repairs what is missing and never
touches your settings or photos.

<details>
<summary>What the installer does</summary>

1. Installs the needed apt packages (picamera2, OpenCV, ffmpeg, ...).
2. Creates the `pirdfy` system user (in the `video` group).
3. Checks out the latest release into `/opt/pirdfy` and builds a Python environment from the
   locked `requirements.txt` (camera and imaging libraries come from apt).
4. Downloads the detection model to `/var/lib/pirdfy/models` (or builds it once on the Pi).
5. Installs `pirdfy.service` (hardened) plus `pirdfy-update.path`, which lets the web
   interface request updates without giving the service root rights.
6. Starts Pirdfy and waits until it reports healthy.

Upgrading from Pirdfy 1.x: the old installation is moved to `/opt/pirdfy-v1-backup-<date>`
(including its photos) and the new version is installed cleanly.
</details>

## Update

**Settings → System → Install update**, or:

```bash
sudo pirdfy update          # latest release (or main, if you chose the development channel)
pirdfy update --check       # only look
```

Each update backs up the database, installs the new code and packages, restarts, and checks
`/healthz`. If the new version does not come up, the previous version, Python environment and
database are restored automatically.

## Catching the arrival (pre-roll)

By default a clip starts when a bird is detected, so the landing is usually missed. With
**Settings → Video → Include the arrival** (or `video.preroll_seconds`, 1–10 s) the camera keeps the
last few seconds of video in memory and every clip starts that much earlier.

The Pi 5 has no hardware video encoder, so this means software H.264 encoding all day, even when
no bird is there. It is off by default. Measure what it costs on your Pi first:

```bash
sudo pirdfy bench               # ~5 minutes; stops and restarts Pirdfy
sudo pirdfy bench --seconds 300 # longer phases give a more honest temperature
```

The bench compares CPU use, temperature, throttling and detection speed with and without
pre-roll, records a test clip to check that the arrival is in it, and recommends whether to turn
it on. Encoding cost scales with pixels per second, so 15 fps or 1280×720 should cost about half
(run the bench again to confirm). The live view shows
"N s pre-roll" while it is active, and *Pi health* shows Pirdfy's own CPU use.

Clips are written as fragmented MP4, so a clip that is cut off (power cut, crash) still plays up to
that point.

## Everyday commands

```text
pirdfy status      service status and dashboard address
pirdfy logs        follow the log         pirdfy logs -n 200   last 200 lines
pirdfy doctor      check camera, model, service, power supply and disk
pirdfy passwd      change the dashboard password
pirdfy config      edit settings in a text editor, validate, restart
pirdfy camera      list connected cameras
pirdfy bench       measure the cost of video pre-roll on this Pi
pirdfy restart | stop | start
```

## Configuration

Almost everything is in **Settings** in the web interface. The same settings live in
`/etc/pirdfy/config.yaml`, which only contains what you changed; every option with its default is
documented in [`config/config.example.yaml`](config/config.example.yaml).

| Path | What |
|---|---|
| `/opt/pirdfy` | code and Python environment (replaced on update) |
| `/etc/pirdfy/config.yaml` | your settings |
| `/var/lib/pirdfy` | database, photos and videos (`media/YYYY/MM/DD`), model, password hash |

### Reaching the dashboard from outside your home

The dashboard always requires a password and blocks cross-site requests. Over plain HTTP the
password travels unencrypted, so for access over the internet use HTTPS: either set
`web.tls_cert` / `web.tls_key`, or put it behind a reverse proxy such as Caddy (and set
`web.trusted_proxies: ["127.0.0.1"]`).

## Troubleshooting

- **No camera / "Camera 0 not detected"**: `pirdfy camera` should list it. Check the ribbon cable
  orientation and reboot. Camera errors are also shown on the dashboard.
- **"Model file not found"**: `sudo pirdfy model`.
- **Under-voltage warnings**: use the official 27 W USB-C supply; the dashboard and
  `pirdfy doctor` show throttling.
- **Forgot the password**: `sudo pirdfy passwd`.

## API

Everything the dashboard does goes through a JSON API (same login cookie). Interactive docs are at
`/api/docs` once you are signed in. Live events (`visit_started`, `visit_ended`, `photo`,
`recording`, `camera`, `warning`) stream from `/api/events` (Server-Sent Events).

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest && ruff check pirdfy tests

# Run without a Pi: mock camera, optionally fed with your own JPEGs
mkdir -p dev-data/models && cp /path/to/yolov8n.onnx dev-data/models/
python -m pirdfy passwd
PIRDFY_CAMERA=mock PIRDFY_MOCK_IMAGES=~/bird-photos python -m pirdfy serve
```

The mock camera is only used when `PIRDFY_CAMERA=mock` is set; on a Pi a missing or broken camera
is reported as an error rather than silently replaced by fake images.

Project layout: `pirdfy/` (camera, motion, detector, pipeline, storage, notify, auth, web),
`deploy/` (systemd units and the `pirdfy` command), `install.sh`, `tests/`.

## License

MIT for Pirdfy itself. The default YOLOv8 model weights are published by Ultralytics under
AGPL-3.0; see their license if you redistribute them.

Inspired by [BirdNET-Pi](https://github.com/mcguirepr89/BirdNET-Pi).
