#!/usr/bin/env python3
"""
Notification support using Apprise.
Supports Pushover and many other services.
"""

import logging
from pathlib import Path
from typing import Optional, List
from dataclasses import dataclass
from datetime import datetime

try:
    import apprise
    APPRISE_AVAILABLE = True
except ImportError:
    APPRISE_AVAILABLE = False
    apprise = None

logger = logging.getLogger(__name__)


@dataclass
class BirdNotification:
    """Data for a bird detection notification."""
    timestamp: datetime
    camera_id: int
    confidence: float
    image_path: Optional[str] = None
    species: Optional[str] = None  # For future bird ID support


class NotificationManager:
    """Manages push notifications for bird detections."""
    
    def __init__(self, config: dict):
        self.config = config.get("notifications", {})
        self.enabled = self.config.get("enabled", False)
        self.apprise = None
        self._last_notification_time: Optional[datetime] = None
        
        # Cooldown between notifications (avoid spam)
        self.cooldown_seconds = self.config.get("cooldown_seconds", 60)
        
        # Whether to attach bird images
        self.attach_images = self.config.get("attach_images", True)
        
        # Notification title template
        self.title_template = self.config.get("title", "Bird Detected! 🐦")
        
        # Notification body template
        self.body_template = self.config.get("body", 
            "A bird was detected at {time} (confidence: {confidence:.0%})")
    
    def initialize(self) -> bool:
        """Initialize the notification system."""
        if not self.enabled:
            logger.info("Notifications disabled in config")
            return True
        
        if not APPRISE_AVAILABLE:
            logger.error("Apprise not installed. Run: pip install apprise")
            return False
        
        # Get notification URLs from config
        urls = self.config.get("urls", [])
        
        if not urls:
            logger.warning("No notification URLs configured")
            return False
        
        try:
            self.apprise = apprise.Apprise()
            
            for url in urls:
                if self.apprise.add(url):
                    # Mask the URL for logging (hide tokens)
                    masked = self._mask_url(url)
                    logger.info(f"Added notification service: {masked}")
                else:
                    logger.warning(f"Failed to add notification URL: {self._mask_url(url)}")
            
            if len(self.apprise) == 0:
                logger.error("No valid notification services configured")
                return False
            
            logger.info(f"Notification system initialized with {len(self.apprise)} service(s)")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize notifications: {e}")
            return False
    
    def _mask_url(self, url: str) -> str:
        """Mask sensitive parts of notification URLs for logging."""
        # Simple masking - hide anything after :// up to @
        if "://" in url:
            prefix, rest = url.split("://", 1)
            if "@" in rest:
                credentials, host = rest.rsplit("@", 1)
                # Mask credentials
                return f"{prefix}://****@{host}"
            else:
                # Mask tokens in the path
                parts = rest.split("/")
                if len(parts) > 1:
                    return f"{prefix}://{parts[0]}/****"
        return url[:20] + "..."
    
    def _can_send_notification(self) -> bool:
        """Check if we can send a notification (respecting cooldown)."""
        if self._last_notification_time is None:
            return True
        
        elapsed = (datetime.now() - self._last_notification_time).total_seconds()
        return elapsed >= self.cooldown_seconds
    
    def notify_bird_detected(self, notification: BirdNotification) -> bool:
        """Send a notification for a bird detection."""
        if not self.enabled or self.apprise is None:
            return False
        
        if not self._can_send_notification():
            logger.debug("Notification skipped (cooldown)")
            return False
        
        try:
            # Format the message
            title = self.title_template
            body = self.body_template.format(
                time=notification.timestamp.strftime("%H:%M:%S"),
                confidence=notification.confidence,
                camera=notification.camera_id,
                species=notification.species or "Unknown"
            )
            
            # Add species to title if available
            if notification.species:
                title = f"{notification.species} Detected! 🐦"
            
            # Prepare attachment if enabled and image exists
            attach = None
            if self.attach_images and notification.image_path:
                image_path = Path(notification.image_path)
                if image_path.exists():
                    attach = apprise.AppriseAttachment()
                    attach.add(str(image_path))
            
            # Send notification
            result = self.apprise.notify(
                title=title,
                body=body,
                attach=attach,
                notify_type=apprise.NotifyType.INFO
            )
            
            if result:
                self._last_notification_time = datetime.now()
                logger.info(f"Bird notification sent successfully")
            else:
                logger.warning("Failed to send bird notification")
            
            return result
            
        except Exception as e:
            logger.error(f"Error sending notification: {e}")
            return False
    
    def send_test_notification(self) -> bool:
        """Send a test notification to verify configuration."""
        if not self.enabled or self.apprise is None:
            logger.warning("Notifications not enabled or not initialized")
            return False
        
        try:
            result = self.apprise.notify(
                title="Pirdfy Test Notification 🐦",
                body="Your notification setup is working correctly!",
                notify_type=apprise.NotifyType.INFO
            )
            
            if result:
                logger.info("Test notification sent successfully")
            else:
                logger.warning("Test notification failed")
            
            return result
            
        except Exception as e:
            logger.error(f"Error sending test notification: {e}")
            return False
    
    def close(self):
        """Clean up resources."""
        self.apprise = None
