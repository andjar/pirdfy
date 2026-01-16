"""
Pirdfy Battery & System Monitoring Module
Monitors battery status, CPU, memory, temperature, and disk usage.
"""

import logging
import threading
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("Warning: psutil not available, system monitoring limited")

logger = logging.getLogger(__name__)


@dataclass
class SystemStatus:
    """Current system status."""
    timestamp: datetime
    
    # CPU
    cpu_percent: float
    cpu_count: int
    cpu_freq_mhz: Optional[float]
    
    # Memory
    memory_total_mb: float
    memory_used_mb: float
    memory_percent: float
    
    # Disk
    disk_total_gb: float
    disk_used_gb: float
    disk_free_gb: float
    disk_percent: float
    
    # Temperature (Raspberry Pi specific)
    cpu_temperature: Optional[float]
    
    # Battery
    battery_percent: Optional[float]
    battery_charging: Optional[bool]
    battery_time_left: Optional[int]  # seconds


class SystemMonitor:
    """
    Monitors system resources including battery status.
    Useful for portable/battery-powered setups.
    """
    
    def __init__(self, config: Dict[str, Any], database=None, data_path: str = "data"):
        self.config = config.get("system", {})
        self.database = database
        self.data_path = data_path
        
        self.battery_monitoring = self.config.get("battery_monitoring", True)
        self.collect_stats = self.config.get("collect_stats", True)
        self.stats_interval = self.config.get("stats_interval", 60)
        
        self._running = False
        self._monitor_thread = None
        self._last_status: Optional[SystemStatus] = None
        
        # Callbacks for low battery warnings
        self._low_battery_callbacks = []
        self._low_battery_threshold = 20  # percent
        self._low_battery_warned = False
    
    def start(self):
        """Start the monitoring thread."""
        if self._running or not self.collect_stats:
            return
        
        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info("System monitor started")
    
    def stop(self):
        """Stop the monitoring thread."""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        logger.info("System monitor stopped")
    
    def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                status = self.get_status()
                self._last_status = status
                
                # Store in database
                if self.database and status:
                    self.database.add_system_stats(
                        cpu_percent=status.cpu_percent,
                        memory_percent=status.memory_percent,
                        disk_percent=status.disk_percent,
                        temperature=status.cpu_temperature,
                        battery_percent=status.battery_percent,
                        battery_charging=status.battery_charging
                    )
                
                # Check for low battery
                if status and status.battery_percent is not None:
                    if status.battery_percent <= self._low_battery_threshold:
                        if not self._low_battery_warned:
                            self._low_battery_warned = True
                            self._trigger_low_battery_warning(status)
                    else:
                        self._low_battery_warned = False
                
                time.sleep(self.stats_interval)
                
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
                time.sleep(10)
    
    def get_status(self) -> Optional[SystemStatus]:
        """Get current system status."""
        if not PSUTIL_AVAILABLE:
            return self._get_mock_status()
        
        try:
            # CPU info
            cpu_percent = psutil.cpu_percent(interval=0.1)
            cpu_count = psutil.cpu_count()
            cpu_freq = psutil.cpu_freq()
            cpu_freq_mhz = cpu_freq.current if cpu_freq else None
            
            # Memory info
            memory = psutil.virtual_memory()
            memory_total_mb = memory.total / (1024 * 1024)
            memory_used_mb = memory.used / (1024 * 1024)
            memory_percent = memory.percent
            
            # Disk info
            disk = psutil.disk_usage(self.data_path)
            disk_total_gb = disk.total / (1024 * 1024 * 1024)
            disk_used_gb = disk.used / (1024 * 1024 * 1024)
            disk_free_gb = disk.free / (1024 * 1024 * 1024)
            disk_percent = disk.percent
            
            # CPU temperature (Raspberry Pi specific)
            cpu_temperature = self._get_cpu_temperature()
            
            # Battery info
            battery_percent = None
            battery_charging = None
            battery_time_left = None
            
            if self.battery_monitoring:
                battery = psutil.sensors_battery()
                if battery:
                    battery_percent = battery.percent
                    battery_charging = battery.power_plugged
                    battery_time_left = battery.secsleft if battery.secsleft != -1 else None
            
            return SystemStatus(
                timestamp=datetime.now(),
                cpu_percent=cpu_percent,
                cpu_count=cpu_count,
                cpu_freq_mhz=cpu_freq_mhz,
                memory_total_mb=memory_total_mb,
                memory_used_mb=memory_used_mb,
                memory_percent=memory_percent,
                disk_total_gb=disk_total_gb,
                disk_used_gb=disk_used_gb,
                disk_free_gb=disk_free_gb,
                disk_percent=disk_percent,
                cpu_temperature=cpu_temperature,
                battery_percent=battery_percent,
                battery_charging=battery_charging,
                battery_time_left=battery_time_left
            )
            
        except Exception as e:
            logger.error(f"Error getting system status: {e}")
            return None
    
    def _get_mock_status(self) -> SystemStatus:
        """Return mock status for testing."""
        import random
        return SystemStatus(
            timestamp=datetime.now(),
            cpu_percent=random.uniform(10, 50),
            cpu_count=4,
            cpu_freq_mhz=1500.0,
            memory_total_mb=4096,
            memory_used_mb=random.uniform(1000, 2000),
            memory_percent=random.uniform(25, 50),
            disk_total_gb=32,
            disk_used_gb=random.uniform(5, 15),
            disk_free_gb=random.uniform(15, 25),
            disk_percent=random.uniform(20, 50),
            cpu_temperature=random.uniform(40, 60),
            battery_percent=random.uniform(50, 100),
            battery_charging=random.choice([True, False]),
            battery_time_left=random.randint(3600, 14400)
        )
    
    def _get_cpu_temperature(self) -> Optional[float]:
        """Get CPU temperature on Raspberry Pi."""
        try:
            # Try thermal_zone (Linux/Raspberry Pi)
            thermal_path = "/sys/class/thermal/thermal_zone0/temp"
            try:
                with open(thermal_path, "r") as f:
                    temp = int(f.read().strip()) / 1000.0
                    return temp
            except FileNotFoundError:
                pass
            
            # Try psutil sensors
            if hasattr(psutil, "sensors_temperatures"):
                temps = psutil.sensors_temperatures()
                if temps:
                    for name, entries in temps.items():
                        for entry in entries:
                            if entry.current:
                                return entry.current
            
            return None
            
        except Exception:
            return None
    
    def get_last_status(self) -> Optional[SystemStatus]:
        """Get the last collected status."""
        return self._last_status
    
    def add_low_battery_callback(self, callback):
        """Add callback for low battery warning."""
        self._low_battery_callbacks.append(callback)
    
    def set_low_battery_threshold(self, threshold: int):
        """Set low battery warning threshold."""
        self._low_battery_threshold = max(5, min(50, threshold))
    
    def _trigger_low_battery_warning(self, status: SystemStatus):
        """Trigger low battery warning callbacks."""
        logger.warning(f"Low battery warning: {status.battery_percent}%")
        for callback in self._low_battery_callbacks:
            try:
                callback(status)
            except Exception as e:
                logger.error(f"Low battery callback error: {e}")
    
    def get_status_dict(self) -> Dict:
        """Get status as dictionary for API responses."""
        status = self._last_status or self.get_status()
        
        if status is None:
            return {"error": "Unable to get system status"}
        
        return {
            "timestamp": status.timestamp.isoformat(),
            "cpu": {
                "percent": round(status.cpu_percent, 1),
                "count": status.cpu_count,
                "freq_mhz": status.cpu_freq_mhz,
                "temperature": round(status.cpu_temperature, 1) if status.cpu_temperature else None
            },
            "memory": {
                "total_mb": round(status.memory_total_mb, 1),
                "used_mb": round(status.memory_used_mb, 1),
                "percent": round(status.memory_percent, 1)
            },
            "disk": {
                "total_gb": round(status.disk_total_gb, 1),
                "used_gb": round(status.disk_used_gb, 1),
                "free_gb": round(status.disk_free_gb, 1),
                "percent": round(status.disk_percent, 1)
            },
            "battery": {
                "percent": round(status.battery_percent, 1) if status.battery_percent else None,
                "charging": status.battery_charging,
                "time_left_minutes": round(status.battery_time_left / 60) if status.battery_time_left else None
            } if status.battery_percent is not None else None
        }
    
    def check_storage_space(self, min_free_gb: float = 1.0) -> bool:
        """Check if there's enough free storage space."""
        status = self.get_status()
        if status:
            return status.disk_free_gb >= min_free_gb
        return True  # Assume OK if can't check
    
    def get_uptime(self) -> Optional[float]:
        """Get system uptime in seconds."""
        if PSUTIL_AVAILABLE:
            try:
                boot_time = psutil.boot_time()
                return time.time() - boot_time
            except Exception:
                pass
        return None
    
    def get_uptime_formatted(self) -> str:
        """Get formatted uptime string."""
        uptime = self.get_uptime()
        if uptime is None:
            return "Unknown"
        
        days = int(uptime // 86400)
        hours = int((uptime % 86400) // 3600)
        minutes = int((uptime % 3600) // 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        parts.append(f"{minutes}m")
        
        return " ".join(parts)
