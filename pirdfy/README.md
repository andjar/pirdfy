# 🐦 Pirdfy - Bird Feeder Camera Detector

A Raspberry Pi 5-based bird feeder camera system that captures, detects, and catalogs birds visiting your feeder.

![Pirdfy Dashboard](docs/screenshot.png)

## Features

- 📸 **Automatic Photo Capture** - Configurable interval (1 second default)
- 🔍 **Bird Detection** - AI-powered bird segmentation and detection
- 📹 **Video Mode** - Automatically record video when birds are detected
- 📊 **Statistics Dashboard** - Heatmaps by hour and species
- 📷 **Multi-Camera Support** - Support for 1-2 cameras
- 🔋 **Battery Monitoring** - Track battery status on portable setups
- 📱 **Mobile-Friendly** - Access dashboard from your phone

## Quick Install

```bash
curl -sSL https://raw.githubusercontent.com/yourusername/pirdfy/main/install.sh | bash
```

Or manually:

```bash
git clone https://github.com/yourusername/pirdfy.git
cd pirdfy
chmod +x install.sh
./install.sh
```

## Requirements

- Raspberry Pi 5 (4GB+ RAM recommended)
- Raspberry Pi Camera Module v2/v3 or compatible USB camera
- Python 3.11+
- 32GB+ SD card recommended

## Usage

### Start the Service

```bash
# Start as service (recommended)
sudo systemctl start pirdfy

# Or run directly
cd /opt/pirdfy
source venv/bin/activate
python src/main.py
```

### Access Dashboard

Open your browser and navigate to:
```
http://<raspberry-pi-ip>:8080
```

Or if connecting directly via hotspot:
```
http://pirdfy.local:8080
```

## Configuration

Edit `config/config.yaml`:

```yaml
camera:
  capture_interval: 1  # seconds between photos
  resolution: [1920, 1080]
  cameras:
    - id: 0
      name: "Front Feeder"
      enabled: true
    - id: 1
      name: "Side Feeder"
      enabled: false

detection:
  model: "yolov8n"
  confidence_threshold: 0.5
  
video:
  enabled: true
  duration: 20  # seconds
  cooldown: 10  # seconds between recordings

web:
  host: "0.0.0.0"
  port: 8080
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

## Project Structure

```
pirdfy/
├── install.sh           # Installation script
├── requirements.txt     # Python dependencies
├── config/
│   └── config.yaml      # Configuration
├── src/
│   ├── main.py          # Main entry point
│   ├── camera.py        # Camera handling
│   ├── detector.py      # Bird detection
│   ├── recorder.py      # Video recording
│   ├── database.py      # SQLite database
│   ├── battery.py       # Battery monitoring
│   └── web/
│       ├── app.py       # Flask web server
│       ├── static/      # CSS, JS assets
│       └── templates/   # HTML templates
├── models/              # Detection models
├── data/
│   ├── photos/          # Captured photos
│   ├── birds/           # Cropped bird images
│   └── videos/          # Recorded videos
└── logs/                # Application logs
```

## Inspired By

- [BirdNET-Pi](https://github.com/mcguirepr89/BirdNET-Pi)

## License

MIT License - see LICENSE file
