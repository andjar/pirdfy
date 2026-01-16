"""
Pirdfy Bird Detection Module
Uses YOLOv8 for bird detection and segmentation.
"""

import logging
from pathlib import Path
from typing import Optional, List, Dict, Tuple, Any
from dataclasses import dataclass, field
import threading
from datetime import datetime

import numpy as np
from PIL import Image

try:
    from ultralytics import YOLO
    YOLO_AVAILABLE = True
except ImportError:
    YOLO_AVAILABLE = False
    print("Warning: ultralytics not available, using mock detector")

import cv2

logger = logging.getLogger(__name__)


@dataclass
class Detection:
    """A single bird detection."""
    class_id: int
    class_name: str
    confidence: float
    bbox: Tuple[int, int, int, int]  # x, y, width, height
    center: Tuple[int, int]
    area: int
    cropped_image: Optional[np.ndarray] = None
    cropped_path: Optional[str] = None


@dataclass
class DetectionResult:
    """Result from running detection on an image."""
    success: bool
    detections: List[Detection] = field(default_factory=list)
    annotated_image: Optional[np.ndarray] = None
    processing_time: float = 0.0
    error: Optional[str] = None


class MockDetector:
    """Mock detector for testing without YOLO."""
    
    def __init__(self, confidence_threshold: float = 0.5):
        self.confidence_threshold = confidence_threshold
    
    def detect(self, image: np.ndarray) -> DetectionResult:
        """Generate mock detections (occasionally returns birds)."""
        import random
        
        detections = []
        
        # Randomly generate 0-2 bird detections
        if random.random() > 0.7:  # 30% chance of detection
            num_birds = random.randint(1, 2)
            h, w = image.shape[:2]
            
            for _ in range(num_birds):
                # Random bounding box
                box_w = random.randint(50, min(200, w // 3))
                box_h = random.randint(50, min(200, h // 3))
                x = random.randint(0, w - box_w)
                y = random.randint(0, h - box_h)
                
                detections.append(Detection(
                    class_id=14,  # Bird class in COCO
                    class_name="bird",
                    confidence=random.uniform(0.5, 0.95),
                    bbox=(x, y, box_w, box_h),
                    center=(x + box_w // 2, y + box_h // 2),
                    area=box_w * box_h
                ))
        
        return DetectionResult(
            success=True,
            detections=detections,
            processing_time=0.05
        )


class BirdDetector:
    """YOLOv8-based bird detector."""
    
    # COCO class ID for bird
    BIRD_CLASS_ID = 14
    BIRD_CLASS_NAME = "bird"
    
    def __init__(self, config: Dict[str, Any], birds_dir: str = "data/birds"):
        self.config = config.get("detection", {})
        self.birds_dir = Path(birds_dir)
        self.birds_dir.mkdir(parents=True, exist_ok=True)
        
        self.model_name = self.config.get("model", "yolov8n")
        self.confidence_threshold = self.config.get("confidence_threshold", 0.5)
        self.enable_segmentation = self.config.get("enable_segmentation", True)
        self.target_classes = self.config.get("target_classes", ["bird"])
        
        self._model = None
        self._lock = threading.Lock()
        
        # Color for drawing (bright green)
        self.box_color = (0, 255, 0)
        self.text_color = (255, 255, 255)
    
    def initialize(self) -> bool:
        """Initialize the YOLO model."""
        try:
            with self._lock:
                if YOLO_AVAILABLE:
                    # Download model if needed
                    model_path = f"{self.model_name}.pt"
                    self._model = YOLO(model_path)
                    
                    # Warm up model
                    dummy = np.zeros((640, 640, 3), dtype=np.uint8)
                    self._model.predict(dummy, verbose=False)
                    
                    logger.info(f"Bird detector initialized with {self.model_name}")
                else:
                    self._model = MockDetector(self.confidence_threshold)
                    logger.warning("Using mock detector (YOLO not available)")
                
                return True
                
        except Exception as e:
            logger.error(f"Failed to initialize detector: {e}")
            return False
    
    def detect(self, image: np.ndarray, save_crops: bool = True) -> DetectionResult:
        """
        Run bird detection on an image.
        
        Args:
            image: Input image as numpy array (BGR or RGB)
            save_crops: Whether to save cropped bird images
            
        Returns:
            DetectionResult with detections and annotated image
        """
        if self._model is None:
            return DetectionResult(
                success=False,
                error="Detector not initialized"
            )
        
        try:
            import time
            start_time = time.time()
            
            with self._lock:
                if not YOLO_AVAILABLE:
                    # Use mock detector
                    result = self._model.detect(image)
                    if save_crops and result.detections:
                        self._save_crops(image, result.detections)
                    return result
                
                # Run YOLO detection
                results = self._model.predict(
                    image,
                    conf=self.confidence_threshold,
                    classes=[self.BIRD_CLASS_ID],  # Only detect birds
                    verbose=False
                )
                
                detections = []
                
                for result in results:
                    boxes = result.boxes
                    
                    if boxes is not None and len(boxes) > 0:
                        for box in boxes:
                            # Get bounding box
                            x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                            conf = float(box.conf[0].cpu().numpy())
                            cls_id = int(box.cls[0].cpu().numpy())
                            
                            width = x2 - x1
                            height = y2 - y1
                            
                            detection = Detection(
                                class_id=cls_id,
                                class_name=self.BIRD_CLASS_NAME,
                                confidence=conf,
                                bbox=(x1, y1, width, height),
                                center=(x1 + width // 2, y1 + height // 2),
                                area=width * height
                            )
                            
                            # Crop bird image
                            if self.enable_segmentation:
                                # Add padding
                                pad = 20
                                crop_y1 = max(0, y1 - pad)
                                crop_y2 = min(image.shape[0], y2 + pad)
                                crop_x1 = max(0, x1 - pad)
                                crop_x2 = min(image.shape[1], x2 + pad)
                                
                                detection.cropped_image = image[crop_y1:crop_y2, crop_x1:crop_x2].copy()
                            
                            detections.append(detection)
                
                # Save cropped images
                if save_crops and detections:
                    self._save_crops(image, detections)
                
                # Create annotated image
                annotated = self._annotate_image(image.copy(), detections)
                
                processing_time = time.time() - start_time
                
                return DetectionResult(
                    success=True,
                    detections=detections,
                    annotated_image=annotated,
                    processing_time=processing_time
                )
                
        except Exception as e:
            logger.error(f"Detection error: {e}")
            return DetectionResult(
                success=False,
                error=str(e)
            )
    
    def _save_crops(self, image: np.ndarray, detections: List[Detection]):
        """Save cropped bird images."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        for i, det in enumerate(detections):
            if det.cropped_image is not None:
                filename = f"bird_{timestamp}_{i}_{int(det.confidence * 100)}.jpg"
                filepath = self.birds_dir / filename
                
                # Convert to PIL and save
                crop_rgb = cv2.cvtColor(det.cropped_image, cv2.COLOR_BGR2RGB) \
                    if det.cropped_image.shape[-1] == 3 else det.cropped_image
                img_pil = Image.fromarray(crop_rgb)
                img_pil.save(str(filepath), "JPEG", quality=90)
                
                det.cropped_path = str(filepath)
    
    def _annotate_image(self, image: np.ndarray, detections: List[Detection]) -> np.ndarray:
        """Draw bounding boxes and labels on image."""
        for det in detections:
            x, y, w, h = det.bbox
            
            # Draw bounding box
            cv2.rectangle(image, (x, y), (x + w, y + h), self.box_color, 2)
            
            # Draw label background
            label = f"{det.class_name}: {det.confidence:.2f}"
            font = cv2.FONT_HERSHEY_SIMPLEX
            font_scale = 0.6
            thickness = 2
            
            (label_w, label_h), baseline = cv2.getTextSize(label, font, font_scale, thickness)
            
            cv2.rectangle(
                image,
                (x, y - label_h - 10),
                (x + label_w + 10, y),
                self.box_color,
                -1
            )
            
            # Draw label text
            cv2.putText(
                image,
                label,
                (x + 5, y - 5),
                font,
                font_scale,
                self.text_color,
                thickness
            )
        
        return image
    
    def set_confidence_threshold(self, threshold: float):
        """Update confidence threshold."""
        self.confidence_threshold = max(0.1, min(0.99, threshold))
        logger.info(f"Confidence threshold set to {self.confidence_threshold}")
    
    def get_model_info(self) -> Dict:
        """Get information about the current model."""
        return {
            "model_name": self.model_name,
            "confidence_threshold": self.confidence_threshold,
            "enable_segmentation": self.enable_segmentation,
            "yolo_available": YOLO_AVAILABLE,
            "target_classes": self.target_classes
        }
    
    def close(self):
        """Cleanup detector resources."""
        self._model = None
        logger.info("Bird detector closed")


class DetectionPipeline:
    """
    Pipeline that connects camera capture to bird detection.
    Processes images and stores results in database.
    """
    
    def __init__(self, detector: BirdDetector, database, annotated_dir: str = "data/annotated"):
        self.detector = detector
        self.database = database
        self.annotated_dir = Path(annotated_dir)
        self.annotated_dir.mkdir(parents=True, exist_ok=True)
        
        # Statistics
        self.total_processed = 0
        self.total_birds_detected = 0
        
        # Callbacks for bird detection events
        self._bird_detected_callbacks = []
    
    def add_bird_detected_callback(self, callback):
        """Add callback for when birds are detected."""
        self._bird_detected_callbacks.append(callback)
    
    def process_capture(self, capture_result) -> Optional[DetectionResult]:
        """
        Process a captured image through the detection pipeline.
        
        Args:
            capture_result: CaptureResult from camera
            
        Returns:
            DetectionResult or None if capture failed
        """
        if not capture_result.success or capture_result.image is None:
            return None
        
        try:
            # Run detection
            detection_result = self.detector.detect(capture_result.image, save_crops=True)
            
            if not detection_result.success:
                return detection_result
            
            has_birds = len(detection_result.detections) > 0
            bird_count = len(detection_result.detections)
            
            # Save to database
            photo_id = self.database.add_photo(
                filename=capture_result.filename,
                filepath=capture_result.filepath,
                camera_id=capture_result.camera_id,
                has_birds=has_birds,
                bird_count=bird_count,
                metadata=capture_result.metadata
            )
            
            # Save individual detections
            for det in detection_result.detections:
                self.database.add_detection(
                    photo_id=photo_id,
                    species="unknown",  # Future: species identification
                    confidence=det.confidence,
                    bbox=det.bbox,
                    cropped_image=det.cropped_path
                )
            
            # Save annotated image if birds detected
            if has_birds and detection_result.annotated_image is not None:
                annotated_filename = f"annotated_{capture_result.filename}"
                annotated_path = self.annotated_dir / annotated_filename
                
                img_pil = Image.fromarray(
                    cv2.cvtColor(detection_result.annotated_image, cv2.COLOR_BGR2RGB)
                )
                img_pil.save(str(annotated_path), "JPEG", quality=85)
            
            # Update hourly stats
            now = datetime.now()
            self.database.update_hourly_stats(
                date=now.strftime("%Y-%m-%d"),
                hour=now.hour,
                total_photos=1,
                photos_with_birds=1 if has_birds else 0,
                total_birds=bird_count
            )
            
            # Update internal stats
            self.total_processed += 1
            self.total_birds_detected += bird_count
            
            # Trigger callbacks if birds detected
            if has_birds:
                for callback in self._bird_detected_callbacks:
                    try:
                        callback(capture_result, detection_result, photo_id)
                    except Exception as e:
                        logger.error(f"Bird detected callback error: {e}")
            
            return detection_result
            
        except Exception as e:
            logger.error(f"Pipeline processing error: {e}")
            return DetectionResult(success=False, error=str(e))
    
    def process_batch(self, capture_results: List) -> List[DetectionResult]:
        """Process multiple capture results."""
        results = []
        for capture in capture_results:
            result = self.process_capture(capture)
            if result:
                results.append(result)
        return results
    
    def get_stats(self) -> Dict:
        """Get pipeline statistics."""
        return {
            "total_processed": self.total_processed,
            "total_birds_detected": self.total_birds_detected,
            "detection_rate": (
                self.total_birds_detected / self.total_processed
                if self.total_processed > 0 else 0
            )
        }
