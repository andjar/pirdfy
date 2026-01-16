"""
Pirdfy Camera Module
Handles photo capture from Raspberry Pi cameras (supports 1-2 cameras).
"""

import io
import time
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from dataclasses import dataclass, field
import threading
from queue import Queue, Empty

try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder, MJPEGEncoder
    from picamera2.outputs import FileOutput
    PICAMERA_AVAILABLE = True
except ImportError as e:
    PICAMERA_AVAILABLE = False
    print(f"Warning: picamera2 not available ({e}), using mock camera")
    print("To install picamera2 on Raspberry Pi: sudo apt install python3-picamera2")
except Exception as e:
    PICAMERA_AVAILABLE = False
    print(f"Warning: Error importing picamera2: {e}")
    print("Camera features will use mock implementation")

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


@dataclass
class CameraConfig:
    """Configuration for a single camera."""
    id: int = 0
    name: str = "Camera"
    enabled: bool = True
    exposure: str = "auto"
    white_balance: str = "auto"
    brightness: float = 0.0
    contrast: float = 1.0
    saturation: float = 1.0
    resolution: Tuple[int, int] = (1920, 1080)
    jpeg_quality: int = 85


@dataclass
class CaptureResult:
    """Result from a photo capture."""
    success: bool
    camera_id: int
    filepath: Optional[str] = None
    filename: Optional[str] = None
    timestamp: Optional[datetime] = None
    image: Optional[np.ndarray] = None
    error: Optional[str] = None
    metadata: Dict = field(default_factory=dict)


class MockCamera:
    """Mock camera for development/testing without actual Pi camera."""
    
    def __init__(self, camera_id: int = 0):
        self.camera_id = camera_id
        self.resolution = (1920, 1080)
        self._running = False
    
    def configure(self, config):
        pass
    
    def start(self):
        self._running = True
    
    def stop(self):
        self._running = False
    
    def capture_array(self) -> np.ndarray:
        """Generate a test pattern image."""
        width, height = self.resolution
        # Create a gradient test pattern
        img = np.zeros((height, width, 3), dtype=np.uint8)
        
        # Create gradient
        for y in range(height):
            for x in range(width):
                img[y, x] = [
                    int(255 * x / width),
                    int(255 * y / height),
                    int(128 + 127 * np.sin((x + y) / 50))
                ]
        
        # Add timestamp
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Add camera ID marker
        img[10:50, 10:200] = [255, 255, 255]
        
        return img
    
    def close(self):
        self.stop()


class Camera:
    """Wrapper for a single Raspberry Pi camera."""
    
    def __init__(self, config: CameraConfig, photo_dir: str = "data/photos"):
        self.config = config
        self.photo_dir = Path(photo_dir)
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        
        self._camera = None
        self._lock = threading.Lock()
        self._is_recording = False
        
    def initialize(self) -> bool:
        """Initialize the camera."""
        try:
            with self._lock:
                if PICAMERA_AVAILABLE:
                    self._camera = Picamera2(camera_num=self.config.id)
                    
                    # Configure for still capture
                    camera_config = self._camera.create_still_configuration(
                        main={"size": self.config.resolution},
                        lores={"size": (640, 480)},
                        display="lores"
                    )
                    self._camera.configure(camera_config)
                    
                    # Apply camera settings
                    self._apply_settings()
                    
                    self._camera.start()
                else:
                    self._camera = MockCamera(self.config.id)
                    self._camera.resolution = self.config.resolution
                    self._camera.start()
                
                logger.info(f"Camera {self.config.id} ({self.config.name}) initialized")
                return True
                
        except Exception as e:
            logger.error(f"Failed to initialize camera {self.config.id}: {e}")
            return False
    
    def _apply_settings(self):
        """Apply camera settings from config."""
        if not PICAMERA_AVAILABLE or self._camera is None:
            return
            
        try:
            controls = {}
            
            # Brightness (mapped to exposure compensation)
            if self.config.brightness != 0.0:
                controls["ExposureValue"] = self.config.brightness * 2
            
            # Contrast
            if self.config.contrast != 1.0:
                controls["Contrast"] = self.config.contrast
            
            # Saturation
            if self.config.saturation != 1.0:
                controls["Saturation"] = self.config.saturation
            
            # Exposure mode
            if self.config.exposure == "auto":
                controls["AeEnable"] = True
            else:
                controls["AeEnable"] = False
                # Manual exposure time in microseconds
                try:
                    controls["ExposureTime"] = int(float(self.config.exposure) * 1000)
                except ValueError:
                    pass
            
            # White balance
            wb_modes = {
                "auto": 0,
                "sunlight": 1,
                "cloudy": 2,
                "shade": 3,
                "tungsten": 4,
                "fluorescent": 5
            }
            if self.config.white_balance in wb_modes:
                if self.config.white_balance == "auto":
                    controls["AwbEnable"] = True
                else:
                    controls["AwbEnable"] = False
                    controls["AwbMode"] = wb_modes[self.config.white_balance]
            
            if controls:
                self._camera.set_controls(controls)
                
        except Exception as e:
            logger.warning(f"Error applying camera settings: {e}")
    
    def update_settings(self, **kwargs):
        """Update camera settings dynamically."""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        
        if PICAMERA_AVAILABLE and self._camera:
            self._apply_settings()
    
    def capture(self, save: bool = True) -> CaptureResult:
        """Capture a single photo."""
        if self._camera is None:
            return CaptureResult(
                success=False,
                camera_id=self.config.id,
                error="Camera not initialized"
            )
        
        try:
            with self._lock:
                timestamp = datetime.now()
                
                # Capture image array
                if PICAMERA_AVAILABLE:
                    image = self._camera.capture_array("main")
                    # Convert from RGB to BGR for OpenCV compatibility
                    image = image[:, :, :3]  # Remove alpha if present
                else:
                    image = self._camera.capture_array()
                
                result = CaptureResult(
                    success=True,
                    camera_id=self.config.id,
                    timestamp=timestamp,
                    image=image,
                    metadata={
                        "camera_name": self.config.name,
                        "resolution": self.config.resolution,
                        "exposure": self.config.exposure
                    }
                )
                
                if save:
                    filename = f"cam{self.config.id}_{timestamp.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
                    filepath = self.photo_dir / filename
                    
                    # Save as JPEG
                    img_pil = Image.fromarray(image)
                    img_pil.save(str(filepath), "JPEG", quality=self.config.jpeg_quality)
                    
                    result.filename = filename
                    result.filepath = str(filepath)
                
                return result
                
        except Exception as e:
            logger.error(f"Capture error on camera {self.config.id}: {e}")
            return CaptureResult(
                success=False,
                camera_id=self.config.id,
                error=str(e)
            )
    
    def start_video_recording(self, output_path: str, duration: float = 20.0) -> bool:
        """Start video recording."""
        if self._camera is None or self._is_recording:
            return False
        
        try:
            with self._lock:
                if PICAMERA_AVAILABLE:
                    # Configure for video
                    video_config = self._camera.create_video_configuration(
                        main={"size": self.config.resolution}
                    )
                    self._camera.configure(video_config)
                    
                    encoder = H264Encoder(bitrate=10000000)
                    output = FileOutput(output_path)
                    
                    self._camera.start_recording(encoder, output)
                    self._is_recording = True
                    
                    logger.info(f"Started video recording: {output_path}")
                    return True
                else:
                    logger.warning("Video recording not available in mock mode")
                    return False
                    
        except Exception as e:
            logger.error(f"Failed to start video recording: {e}")
            return False
    
    def stop_video_recording(self):
        """Stop video recording."""
        if not self._is_recording:
            return
        
        try:
            with self._lock:
                if PICAMERA_AVAILABLE and self._camera:
                    self._camera.stop_recording()
                    
                    # Reconfigure for still capture
                    camera_config = self._camera.create_still_configuration(
                        main={"size": self.config.resolution}
                    )
                    self._camera.configure(camera_config)
                    
                self._is_recording = False
                logger.info("Stopped video recording")
                
        except Exception as e:
            logger.error(f"Error stopping video recording: {e}")
    
    def is_recording(self) -> bool:
        """Check if camera is currently recording video."""
        return self._is_recording
    
    def close(self):
        """Close the camera."""
        if self._camera:
            try:
                if self._is_recording:
                    self.stop_video_recording()
                self._camera.close()
                self._camera = None
                logger.info(f"Camera {self.config.id} closed")
            except Exception as e:
                logger.error(f"Error closing camera: {e}")


class CameraManager:
    """Manager for multiple cameras."""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.cameras: Dict[int, Camera] = {}
        self.photo_dir = Path(config.get("storage", {}).get("data_path", "data")) / "photos"
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        
        self._capture_interval = config.get("camera", {}).get("capture_interval", 1)
        self._running = False
        self._capture_thread = None
        self._capture_queue = Queue()
        
        # Callbacks
        self._on_capture_callbacks = []
    
    def initialize(self) -> bool:
        """Initialize all configured cameras."""
        camera_config = self.config.get("camera", {})
        resolution = tuple(camera_config.get("resolution", [1920, 1080]))
        jpeg_quality = camera_config.get("jpeg_quality", 85)
        
        cameras_config = camera_config.get("cameras", [{"id": 0, "name": "Primary", "enabled": True}])
        
        success = True
        for cam_cfg in cameras_config:
            if not cam_cfg.get("enabled", False):
                continue
            
            config = CameraConfig(
                id=cam_cfg.get("id", 0),
                name=cam_cfg.get("name", f"Camera {cam_cfg.get('id', 0)}"),
                enabled=cam_cfg.get("enabled", True),
                exposure=cam_cfg.get("exposure", "auto"),
                white_balance=cam_cfg.get("white_balance", "auto"),
                brightness=cam_cfg.get("brightness", 0.0),
                contrast=cam_cfg.get("contrast", 1.0),
                saturation=cam_cfg.get("saturation", 1.0),
                resolution=resolution,
                jpeg_quality=jpeg_quality
            )
            
            camera = Camera(config, str(self.photo_dir))
            if camera.initialize():
                self.cameras[config.id] = camera
            else:
                success = False
        
        logger.info(f"Initialized {len(self.cameras)} camera(s)")
        return success and len(self.cameras) > 0
    
    def get_camera(self, camera_id: int) -> Optional[Camera]:
        """Get a specific camera."""
        return self.cameras.get(camera_id)
    
    def capture_all(self, save: bool = True) -> List[CaptureResult]:
        """Capture from all cameras."""
        results = []
        for camera in self.cameras.values():
            result = camera.capture(save=save)
            results.append(result)
        return results
    
    def capture_single(self, camera_id: int, save: bool = True) -> CaptureResult:
        """Capture from a specific camera."""
        camera = self.cameras.get(camera_id)
        if camera is None:
            return CaptureResult(
                success=False,
                camera_id=camera_id,
                error=f"Camera {camera_id} not found"
            )
        return camera.capture(save=save)
    
    def add_capture_callback(self, callback):
        """Add callback to be called on each capture."""
        self._on_capture_callbacks.append(callback)
    
    def set_capture_interval(self, interval: float):
        """Update capture interval."""
        self._capture_interval = max(0.1, interval)
        logger.info(f"Capture interval set to {self._capture_interval}s")
    
    def get_capture_interval(self) -> float:
        """Get current capture interval."""
        return self._capture_interval
    
    def start_continuous_capture(self):
        """Start continuous photo capture in background thread."""
        if self._running:
            return
        
        self._running = True
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        logger.info("Started continuous capture")
    
    def stop_continuous_capture(self):
        """Stop continuous capture."""
        self._running = False
        if self._capture_thread:
            self._capture_thread.join(timeout=5)
        logger.info("Stopped continuous capture")
    
    def _capture_loop(self):
        """Main capture loop."""
        while self._running:
            try:
                start_time = time.time()
                
                results = self.capture_all(save=True)
                
                # Call callbacks
                for callback in self._on_capture_callbacks:
                    try:
                        callback(results)
                    except Exception as e:
                        logger.error(f"Capture callback error: {e}")
                
                # Sleep for remaining interval time
                elapsed = time.time() - start_time
                sleep_time = max(0, self._capture_interval - elapsed)
                time.sleep(sleep_time)
                
            except Exception as e:
                logger.error(f"Capture loop error: {e}")
                time.sleep(1)  # Prevent tight loop on error
    
    def update_camera_settings(self, camera_id: int, **settings):
        """Update settings for a specific camera."""
        camera = self.cameras.get(camera_id)
        if camera:
            camera.update_settings(**settings)
            return True
        return False
    
    def get_camera_info(self) -> List[Dict]:
        """Get info about all cameras."""
        info = []
        for camera in self.cameras.values():
            info.append({
                "id": camera.config.id,
                "name": camera.config.name,
                "enabled": camera.config.enabled,
                "resolution": camera.config.resolution,
                "exposure": camera.config.exposure,
                "white_balance": camera.config.white_balance,
                "brightness": camera.config.brightness,
                "contrast": camera.config.contrast,
                "saturation": camera.config.saturation,
                "is_recording": camera.is_recording()
            })
        return info
    
    def close(self):
        """Close all cameras."""
        self.stop_continuous_capture()
        for camera in self.cameras.values():
            camera.close()
        self.cameras.clear()
        logger.info("Camera manager closed")
