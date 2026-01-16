# 🐦 Pirdfy - Bird Feeder Camera Detector

A Raspberry Pi 5-based bird feeder camera system that captures, detects, and catalogs birds visiting your feeder.

## Features

- 📸 **Automatic Photo Capture** - Configurable interval (1 second default)
- 🔍 **Bird Detection** - YOLOv8-powered bird detection and segmentation
- 📹 **Video Mode** - Automatically record video when birds are detected
- 📊 **Statistics Dashboard** - Heatmaps by hour and species
- 📷 **Multi-Camera Support** - Support for 1-2 Raspberry Pi cameras
- 🔋 **Battery Monitoring** - Track battery status on portable setups
- 📱 **Mobile-Friendly** - Access dashboard from your phone
- 🔒 **Security** - Runs as dedicated `pirdfy` user (not root)

## Requirements

- Raspberry Pi 5 (4GB+ RAM recommended)
- **Raspberry Pi Camera Module v2/v3** (uses picamera2/libcamera)
- Debian Trixie / Raspberry Pi OS Bookworm or newer
- Python 3.11+
- 32GB+ SD card recommended

## Installation

### Option 1: From GitHub (after pushing to repo)

```bash
# Clone and install
git clone https://github.com/andjar/pirdfy.git
cd pirdfy
sudo chmod +x install.sh
sudo ./install.sh
```

### Option 2: One-liner (after pushing to repo)

```bash
curl -sSL https://raw.githubusercontent.com/andjar/pirdfy/main/install.sh | sudo bash
```

### Option 3: Manual installation (local files)

```bash
# Copy project to your Pi, then:
cd pirdfy
sudo chmod +x install.sh
sudo ./install.sh
```

## What the installer does

1. Creates a dedicated `pirdfy` system user (security)
2. Installs system dependencies (libcamera, picamera2, etc.)
3. Sets up Python virtual environment with system packages
4. Downloads YOLOv8 bird detection model
5. Creates systemd service for auto-start
6. Sets up proper file permissions

## Usage

### Commands

```bash
pirdfy start     # Start the service
pirdfy stop      # Stop the service
pirdfy restart   # Restart the service
pirdfy status    # Check service status
pirdfy logs      # View live logs (Ctrl+C to exit)
pirdfy camera    # Test camera connectivity
pirdfy config    # Edit configuration
pirdfy update    # Update to latest version
pirdfy fix       # Fix file permissions
pirdfy url       # Show dashboard URL
pirdfy help      # Show all commands
```

### Access Dashboard

Open your browser and navigate to:
```
http://<raspberry-pi-ip>:8080
```

Find your Pi's IP with: `hostname -I`

## Configuration

Edit `/opt/pirdfy/config/config.yaml`:

```yaml
camera:
  capture_interval: 1  # seconds between photos
  resolution: [1920, 1080]
  cameras:
    - id: 0
      name: "Bird Feeder"
      enabled: true
      exposure: auto
      white_balance: auto

detection:
  model: "yolov8n"
  confidence_threshold: 0.5
  
video:
  enabled: true
  duration: 20  # seconds to record
  cooldown: 10  # seconds between recordings

web:
  host: "0.0.0.0"
  port: 8080
```

## Troubleshooting

### Camera not detected

```bash
# Test camera
pirdfy camera

# Or manually:
rpicam-hello --list-cameras
```

### Permission issues

```bash
# Fix permissions
pirdfy fix

# Check pirdfy user is in video group
groups pirdfy
```

### Service won't start

```bash
# Check logs
pirdfy logs

# Check service status
pirdfy status
```

### After reboot camera not working

You may need to enable the camera interface:
```bash
sudo raspi-config
# Interface Options -> Camera -> Enable
sudo reboot
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/photos` | GET | List recent photos |
| `/api/photos/<id>` | GET | Get photo details |
| `/api/birds` | GET | List detected birds |
| `/api/stats/hourly` | GET | Hourly detection heatmap |
| `/api/stats/species` | GET | Species statistics |
| `/api/config` | GET/POST | Get/update configuration |
| `/api/camera/settings` | GET/POST | Camera settings |
| `/api/status` | GET | System status (battery, etc.) |
| `/api/control/start` | POST | Start capture |
| `/api/control/stop` | POST | Stop capture |

## Project Structure

```
/opt/pirdfy/
├── config/
│   └── config.yaml      # Configuration
├── src/
│   ├── main.py          # Main entry point
│   ├── camera.py        # Raspberry Pi camera handling
│   ├── detector.py      # YOLOv8 bird detection
│   ├── recorder.py      # Video recording
│   ├── database.py      # SQLite database
│   ├── battery.py       # Battery monitoring
│   └── web/
│       ├── app.py       # Flask web server
│       └── templates/   # HTML templates
├── data/
│   ├── photos/          # Captured photos
│   ├── birds/           # Cropped bird images
│   ├── videos/          # Recorded videos
│   └── pirdfy.db        # SQLite database
├── logs/                # Application logs
├── models/              # Detection models
└── venv/                # Python virtual environment
```

## Security

- Runs as dedicated `pirdfy` user (not root)
- Systemd security hardening enabled
- No external network access required
- All data stored locally

## Inspired By

- [BirdNET-Pi](https://github.com/mcguirepr89/BirdNET-Pi)

## License

MIT License
