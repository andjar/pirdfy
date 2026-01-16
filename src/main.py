#!/usr/bin/env python3
"""
Pirdfy - Bird Feeder Camera Detector
Main entry point and application orchestration.
"""

import os
import sys
import signal
import logging
import argparse
from pathlib import Path
from typing import Optional
import threading
import time

import yaml

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from database import get_database
from camera import CameraManager
from detector import BirdDetector, DetectionPipeline
from recorder import VideoRecorder, create_bird_detection_handler
from battery import SystemMonitor
from web.app import create_app, run_server


# Configure logging
def setup_logging(config: dict):
    """Set up logging based on configuration."""
    log_config = config.get("logging", {})
    log_level = getattr(logging, log_config.get("level", "INFO").upper())
    log_file = log_config.get("file", "logs/pirdfy.log")
    
    # Create logs directory with proper permissions
    log_dir = Path(log_file).parent
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        # Try to set permissions (may fail if not root)
        os.chmod(str(log_dir), 0o755)
    except PermissionError:
        print(f"Warning: Could not set permissions on {log_dir}")
    
    # Set up handlers
    handlers = [logging.StreamHandler()]
    
    # Try to add file handler
    try:
        file_handler = logging.FileHandler(log_file)
        handlers.append(file_handler)
    except PermissionError:
        print(f"Warning: Cannot write to log file {log_file}, using console only")
    except Exception as e:
        print(f"Warning: Error setting up log file: {e}")
    
    # Configure logging
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers
    )
    
    # Reduce noise from other libraries
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    logging.getLogger("ultralytics").setLevel(logging.WARNING)


def load_config(config_path: str = "config/config.yaml") -> dict:
    """Load configuration from YAML file."""
    config_file = Path(config_path)
    
    if not config_file.exists():
        logging.warning(f"Config file not found: {config_path}, using defaults")
        return {}
    
    with open(config_file, "r") as f:
        return yaml.safe_load(f) or {}


class Pirdfy:
    """Main application class that orchestrates all components."""
    
    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Create data directories
        self._setup_directories()
        
        # Initialize components
        self.database = None
        self.camera_manager = None
        self.detector = None
        self.pipeline = None
        self.video_recorder = None
        self.system_monitor = None
        self.web_app = None
        self.socketio = None
        
        self._running = False
        self._shutdown_event = threading.Event()
    
    def _setup_directories(self):
        """Create required data directories."""
        data_path = Path(self.config.get("storage", {}).get("data_path", "data"))
        
        directories = [
            data_path,
            data_path / "photos",
            data_path / "birds",
            data_path / "videos",
            data_path / "annotated",
            Path("logs"),
            Path("models")
        ]
        
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
    
    def initialize(self) -> bool:
        """Initialize all components."""
        self.logger.info("Initializing Pirdfy...")
        
        try:
            # Database
            data_path = self.config.get("storage", {}).get("data_path", "data")
            self.database = get_database(f"{data_path}/pirdfy.db")
            self.logger.info("Database initialized")
            
            # Camera Manager - continue even if no cameras found
            self.camera_manager = CameraManager(self.config)
            if not self.camera_manager.initialize():
                self.logger.warning("No cameras initialized - running in web-only mode")
                self.logger.warning("Camera features will be unavailable")
            else:
                self.logger.info(f"Camera manager initialized with {len(self.camera_manager.cameras)} camera(s)")
            
            # Bird Detector - continue even if detector fails
            self.detector = BirdDetector(
                self.config,
                birds_dir=f"{data_path}/birds"
            )
            if not self.detector.initialize():
                self.logger.warning("Bird detector not initialized - detection disabled")
            else:
                self.logger.info("Bird detector initialized")
            
            # Detection Pipeline
            self.pipeline = DetectionPipeline(
                self.detector,
                self.database,
                annotated_dir=f"{data_path}/annotated"
            )
            self.logger.info("Detection pipeline initialized")
            
            # Video Recorder
            self.video_recorder = VideoRecorder(
                self.config,
                self.camera_manager,
                self.database,
                videos_dir=f"{data_path}/videos"
            )
            self.logger.info("Video recorder initialized")
            
            # System Monitor
            self.system_monitor = SystemMonitor(
                self.config,
                self.database,
                data_path=data_path
            )
            self.logger.info("System monitor initialized")
            
            # Web Application
            self.web_app, self.socketio = create_app(
                self.config,
                camera_manager=self.camera_manager,
                detector=self.detector,
                pipeline=self.pipeline,
                video_recorder=self.video_recorder,
                system_monitor=self.system_monitor,
                database=self.database
            )
            self.logger.info("Web application initialized")
            
            # Wire up components
            self._connect_components()
            
            self.logger.info("Pirdfy initialized successfully")
            return True
            
        except Exception as e:
            self.logger.exception(f"Initialization failed: {e}")
            return False
    
    def _connect_components(self):
        """Connect components via callbacks."""
        
        # Only connect camera callbacks if we have cameras
        if self.camera_manager and self.camera_manager.cameras:
            # Camera capture -> Detection pipeline
            def on_capture(capture_results):
                for result in capture_results:
                    if self.pipeline:
                        detection_result = self.pipeline.process_capture(result)
                    else:
                        detection_result = None
                    
                    # Emit WebSocket events
                    if self.web_app and result.success:
                        self.web_app.emit_new_photo({
                            "id": result.filepath,
                            "filename": result.filename,
                            "camera_id": result.camera_id,
                            "timestamp": result.timestamp.isoformat() if result.timestamp else None
                        })
                        
                        if detection_result and detection_result.detections:
                            for det in detection_result.detections:
                                # Convert numpy types to native Python for JSON
                                bbox = tuple(int(x) for x in det.bbox)
                                self.web_app.emit_bird_detected({
                                    "confidence": float(det.confidence),
                                    "bbox": bbox,
                                    "cropped_image": det.cropped_path
                                })
            
            self.camera_manager.add_capture_callback(on_capture)
            
            # Bird detection -> Video recording
            if self.pipeline and self.video_recorder:
                bird_handler = create_bird_detection_handler(self.video_recorder)
                self.pipeline.add_bird_detected_callback(bird_handler)
        
        # Video recording events
        if self.web_app:
            self.video_recorder.on_recording_start(
                lambda job: self.web_app.emit_recording_started({
                    "camera_id": job.camera_id,
                    "duration": job.duration
                })
            )
            self.video_recorder.on_recording_end(
                lambda job: self.web_app.emit_recording_ended({
                    "camera_id": job.camera_id,
                    "completed": job.completed,
                    "output_path": job.output_path
                })
            )
        
        # Low battery warning
        def on_low_battery(status):
            self.logger.warning(f"Low battery: {status.battery_percent}%")
            if self.web_app:
                self.web_app.emit_status_update({
                    "warning": "low_battery",
                    "battery_percent": status.battery_percent
                })
        
        self.system_monitor.add_low_battery_callback(on_low_battery)
    
    def start(self):
        """Start all services."""
        self.logger.info("Starting Pirdfy services...")
        self._running = True
        
        try:
            # Start system monitor
            if self.system_monitor:
                self.system_monitor.start()
            
            # Start video recorder (only if we have cameras)
            if self.video_recorder and self.camera_manager and self.camera_manager.cameras:
                self.video_recorder.start()
            
            # Start camera capture (only if we have cameras)
            if self.camera_manager and self.camera_manager.cameras:
                self.camera_manager.start_continuous_capture()
                self.logger.info("Camera capture started")
            else:
                self.logger.warning("No cameras available - capture disabled")
            
            # Start web server (blocking)
            web_config = self.config.get("web", {})
            host = web_config.get("host", "0.0.0.0")
            port = web_config.get("port", 8080)
            
            self.logger.info(f"Starting web server on {host}:{port}")
            self.logger.info(f"Dashboard available at http://{host}:{port}")
            
            run_server(self.web_app, self.socketio, host=host, port=port)
            
        except KeyboardInterrupt:
            self.logger.info("Received interrupt signal")
        finally:
            self.stop()
    
    def stop(self):
        """Stop all services."""
        if not self._running:
            return
        
        self.logger.info("Stopping Pirdfy services...")
        self._running = False
        
        # Stop in reverse order of startup
        if self.camera_manager:
            self.camera_manager.stop_continuous_capture()
            self.camera_manager.close()
        
        if self.video_recorder:
            self.video_recorder.stop()
        
        if self.system_monitor:
            self.system_monitor.stop()
        
        if self.detector:
            self.detector.close()
        
        self._shutdown_event.set()
        self.logger.info("Pirdfy stopped")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Pirdfy - Bird Feeder Camera Detector")
    parser.add_argument(
        "-c", "--config",
        default="config/config.yaml",
        help="Path to configuration file"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Web server host (overrides config)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Web server port (overrides config)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode"
    )
    args = parser.parse_args()
    
    # Load configuration
    config = load_config(args.config)
    
    # Override with command line args
    if args.host:
        config.setdefault("web", {})["host"] = args.host
    if args.port:
        config.setdefault("web", {})["port"] = args.port
    if args.debug:
        config.setdefault("web", {})["debug"] = True
        config.setdefault("logging", {})["level"] = "DEBUG"
    
    # Setup logging
    setup_logging(config)
    
    # Create and run application
    app = Pirdfy(config)
    
    # Handle signals
    def signal_handler(sig, frame):
        app.stop()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Initialize and start
    if app.initialize():
        app.start()
    else:
        logging.error("Failed to initialize Pirdfy")
        sys.exit(1)


if __name__ == "__main__":
    main()
