"""
Pirdfy Video Recorder Module
Handles video recording when birds are detected.
"""

import logging
import threading
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass
from queue import Queue, Empty
import os

logger = logging.getLogger(__name__)


@dataclass
class RecordingJob:
    """A video recording job."""
    camera_id: int
    trigger_photo_id: int
    duration: float
    output_path: str
    started_at: Optional[datetime] = None
    completed: bool = False


class VideoRecorder:
    """
    Video recorder that activates when birds are detected.
    Records for a configurable duration with cooldown between recordings.
    """
    
    def __init__(self, config: Dict[str, Any], camera_manager, database,
                 videos_dir: str = "data/videos"):
        self.config = config.get("video", {})
        self.camera_manager = camera_manager
        self.database = database
        self.videos_dir = Path(videos_dir)
        self.videos_dir.mkdir(parents=True, exist_ok=True)
        
        # Configuration
        self.enabled = self.config.get("enabled", True)
        self.duration = self.config.get("duration", 20)  # seconds
        self.cooldown = self.config.get("cooldown", 10)  # seconds
        self.resolution = tuple(self.config.get("resolution", [1920, 1080]))
        self.fps = self.config.get("fps", 30)
        
        # State
        self._recording = False
        self._last_recording_end = 0
        self._current_job: Optional[RecordingJob] = None
        self._lock = threading.Lock()
        
        # Recording queue and thread
        self._job_queue = Queue()
        self._running = False
        self._worker_thread = None
        
        # Callbacks
        self._on_recording_start: Optional[Callable] = None
        self._on_recording_end: Optional[Callable] = None
    
    def start(self):
        """Start the video recorder worker thread."""
        if self._running:
            return
        
        self._running = True
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        logger.info("Video recorder started")
    
    def stop(self):
        """Stop the video recorder."""
        self._running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=5)
        logger.info("Video recorder stopped")
    
    def trigger_recording(self, camera_id: int, trigger_photo_id: int) -> bool:
        """
        Trigger a video recording.
        Returns False if recording is disabled or in cooldown.
        """
        if not self.enabled:
            return False
        
        with self._lock:
            # Check if already recording
            if self._recording:
                logger.debug("Recording already in progress, skipping trigger")
                return False
            
            # Check cooldown
            time_since_last = time.time() - self._last_recording_end
            if time_since_last < self.cooldown:
                logger.debug(f"In cooldown period, {self.cooldown - time_since_last:.1f}s remaining")
                return False
        
        # Create recording job
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"video_cam{camera_id}_{timestamp}.h264"
        output_path = str(self.videos_dir / filename)
        
        job = RecordingJob(
            camera_id=camera_id,
            trigger_photo_id=trigger_photo_id,
            duration=self.duration,
            output_path=output_path
        )
        
        self._job_queue.put(job)
        logger.info(f"Video recording triggered for camera {camera_id}")
        return True
    
    def _worker_loop(self):
        """Worker thread that processes recording jobs."""
        while self._running:
            try:
                job = self._job_queue.get(timeout=1)
                self._process_job(job)
            except Empty:
                continue
            except Exception as e:
                logger.error(f"Recording worker error: {e}")
    
    def _process_job(self, job: RecordingJob):
        """Process a single recording job."""
        with self._lock:
            self._recording = True
            self._current_job = job
        
        try:
            job.started_at = datetime.now()
            logger.info(f"Starting video recording: {job.output_path}")
            
            # Notify start callback
            if self._on_recording_start:
                try:
                    self._on_recording_start(job)
                except Exception as e:
                    logger.error(f"Recording start callback error: {e}")
            
            # Get camera and start recording
            camera = self.camera_manager.get_camera(job.camera_id)
            if camera is None:
                logger.error(f"Camera {job.camera_id} not found")
                return
            
            # Start recording
            if camera.start_video_recording(job.output_path, job.duration):
                # Wait for recording duration
                time.sleep(job.duration)
                
                # Stop recording
                camera.stop_video_recording()
                
                job.completed = True
                
                # Get file size
                filesize = 0
                if os.path.exists(job.output_path):
                    filesize = os.path.getsize(job.output_path)
                
                # Save to database
                self.database.add_video(
                    filename=os.path.basename(job.output_path),
                    filepath=job.output_path,
                    camera_id=job.camera_id,
                    duration=job.duration,
                    trigger_photo_id=job.trigger_photo_id,
                    filesize=filesize
                )
                
                logger.info(f"Video recording completed: {job.output_path}")
            else:
                logger.warning("Failed to start video recording")
            
            # Notify end callback
            if self._on_recording_end:
                try:
                    self._on_recording_end(job)
                except Exception as e:
                    logger.error(f"Recording end callback error: {e}")
            
        finally:
            with self._lock:
                self._recording = False
                self._current_job = None
                self._last_recording_end = time.time()
    
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._recording
    
    def get_current_job(self) -> Optional[RecordingJob]:
        """Get the current recording job."""
        return self._current_job
    
    def set_enabled(self, enabled: bool):
        """Enable or disable video recording."""
        self.enabled = enabled
        logger.info(f"Video recording {'enabled' if enabled else 'disabled'}")
    
    def set_duration(self, duration: float):
        """Set recording duration."""
        self.duration = max(5, min(300, duration))  # 5s to 5min
        logger.info(f"Recording duration set to {self.duration}s")
    
    def set_cooldown(self, cooldown: float):
        """Set cooldown period."""
        self.cooldown = max(0, min(300, cooldown))  # 0 to 5min
        logger.info(f"Recording cooldown set to {self.cooldown}s")
    
    def on_recording_start(self, callback: Callable):
        """Set callback for recording start."""
        self._on_recording_start = callback
    
    def on_recording_end(self, callback: Callable):
        """Set callback for recording end."""
        self._on_recording_end = callback
    
    def get_status(self) -> Dict:
        """Get recorder status."""
        with self._lock:
            cooldown_remaining = 0
            if not self._recording:
                time_since_last = time.time() - self._last_recording_end
                cooldown_remaining = max(0, self.cooldown - time_since_last)
            
            return {
                "enabled": self.enabled,
                "recording": self._recording,
                "duration": self.duration,
                "cooldown": self.cooldown,
                "cooldown_remaining": cooldown_remaining,
                "current_job": {
                    "camera_id": self._current_job.camera_id,
                    "started_at": self._current_job.started_at.isoformat() if self._current_job.started_at else None,
                    "duration": self._current_job.duration
                } if self._current_job else None
            }
    
    def get_config(self) -> Dict:
        """Get recorder configuration."""
        return {
            "enabled": self.enabled,
            "duration": self.duration,
            "cooldown": self.cooldown,
            "resolution": self.resolution,
            "fps": self.fps
        }


def create_bird_detection_handler(video_recorder: VideoRecorder):
    """
    Create a callback handler for bird detection events.
    This connects the detection pipeline to the video recorder.
    """
    def handle_bird_detected(capture_result, detection_result, photo_id):
        """Called when birds are detected in a photo."""
        if detection_result.detections:
            # Trigger recording on the same camera
            video_recorder.trigger_recording(
                camera_id=capture_result.camera_id,
                trigger_photo_id=photo_id
            )
    
    return handle_bird_detected
