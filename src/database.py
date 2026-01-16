"""
Pirdfy Database Module
SQLite database for storing bird detections, photos, and statistics.
"""

import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
import threading


def convert_to_serializable(obj):
    """Convert numpy/sqlite types to JSON-serializable Python types."""
    if obj is None:
        return None
    if isinstance(obj, bytes):
        return obj.decode('utf-8', errors='replace')
    if isinstance(obj, (int, float, str, bool)):
        return obj
    if hasattr(obj, 'item'):  # numpy scalar
        return obj.item()
    if hasattr(obj, 'isoformat'):  # datetime objects
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): convert_to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [convert_to_serializable(v) for v in obj]
    # For anything else, convert to string
    return str(obj)


class Database:
    """Thread-safe SQLite database handler for Pirdfy."""
    
    def __init__(self, db_path: str = "data/pirdfy.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()
    
    @contextmanager
    def _get_connection(self):
        """Get thread-local database connection."""
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            self._local.conn = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False
            )
            self._local.conn.row_factory = sqlite3.Row
        try:
            yield self._local.conn
        except Exception as e:
            self._local.conn.rollback()
            raise e
    
    def _init_db(self):
        """Initialize database schema."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Photos table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS photos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    camera_id INTEGER DEFAULT 0,
                    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    has_birds BOOLEAN DEFAULT FALSE,
                    bird_count INTEGER DEFAULT 0,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Bird detections table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS detections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    photo_id INTEGER,
                    species TEXT DEFAULT 'unknown',
                    confidence REAL,
                    bbox_x INTEGER,
                    bbox_y INTEGER,
                    bbox_width INTEGER,
                    bbox_height INTEGER,
                    cropped_image TEXT,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (photo_id) REFERENCES photos (id) ON DELETE CASCADE
                )
            """)
            
            # Videos table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT NOT NULL,
                    filepath TEXT NOT NULL,
                    camera_id INTEGER DEFAULT 0,
                    duration REAL,
                    trigger_photo_id INTEGER,
                    recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    filesize INTEGER,
                    FOREIGN KEY (trigger_photo_id) REFERENCES photos (id)
                )
            """)
            
            # Statistics table (hourly aggregates)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS hourly_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date DATE NOT NULL,
                    hour INTEGER NOT NULL,
                    total_photos INTEGER DEFAULT 0,
                    photos_with_birds INTEGER DEFAULT 0,
                    total_birds INTEGER DEFAULT 0,
                    species_counts TEXT,
                    UNIQUE(date, hour)
                )
            """)
            
            # System stats table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    cpu_percent REAL,
                    memory_percent REAL,
                    disk_percent REAL,
                    temperature REAL,
                    battery_percent REAL,
                    battery_charging BOOLEAN
                )
            """)
            
            # Create indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_photos_captured_at ON photos(captured_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_photos_has_birds ON photos(has_birds)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_detections_photo_id ON detections(photo_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_detections_species ON detections(species)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_hourly_stats_date ON hourly_stats(date)")
            
            conn.commit()
    
    # Photo methods
    def add_photo(self, filename: str, filepath: str, camera_id: int = 0,
                  has_birds: bool = False, bird_count: int = 0,
                  metadata: Optional[Dict] = None) -> int:
        """Add a photo record."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO photos (filename, filepath, camera_id, has_birds, bird_count, metadata)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (filename, filepath, camera_id, has_birds, bird_count,
                  json.dumps(metadata) if metadata else None))
            conn.commit()
            return cursor.lastrowid
    
    def update_photo(self, photo_id: int, **kwargs):
        """Update photo record."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            allowed_fields = ['has_birds', 'bird_count', 'metadata']
            updates = []
            values = []
            for field, value in kwargs.items():
                if field in allowed_fields:
                    updates.append(f"{field} = ?")
                    if field == 'metadata':
                        value = json.dumps(value) if value else None
                    values.append(value)
            if updates:
                values.append(photo_id)
                cursor.execute(f"UPDATE photos SET {', '.join(updates)} WHERE id = ?", values)
                conn.commit()
    
    def get_photo(self, photo_id: int) -> Optional[Dict]:
        """Get photo by ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM photos WHERE id = ?", (photo_id,))
            row = cursor.fetchone()
            return convert_to_serializable(dict(row)) if row else None
    
    def get_recent_photos(self, limit: int = 100, with_birds_only: bool = False,
                          camera_id: Optional[int] = None) -> List[Dict]:
        """Get recent photos."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM photos WHERE 1=1"
            params = []
            if with_birds_only:
                query += " AND has_birds = TRUE"
            if camera_id is not None:
                query += " AND camera_id = ?"
                params.append(camera_id)
            query += " ORDER BY captured_at DESC LIMIT ?"
            params.append(limit)
            cursor.execute(query, params)
            return [convert_to_serializable(dict(row)) for row in cursor.fetchall()]
    
    # Detection methods
    def add_detection(self, photo_id: int, species: str = "unknown",
                     confidence: float = 0.0, bbox: tuple = (0, 0, 0, 0),
                     cropped_image: Optional[str] = None) -> int:
        """Add a bird detection record."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO detections 
                (photo_id, species, confidence, bbox_x, bbox_y, bbox_width, bbox_height, cropped_image)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (photo_id, species, confidence, *bbox, cropped_image))
            conn.commit()
            return cursor.lastrowid
    
    def get_detections_for_photo(self, photo_id: int) -> List[Dict]:
        """Get all detections for a photo."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM detections WHERE photo_id = ?", (photo_id,))
            return [convert_to_serializable(dict(row)) for row in cursor.fetchall()]
    
    def get_recent_detections(self, limit: int = 50) -> List[Dict]:
        """Get recent bird detections with photo info."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT d.*, p.filename as photo_filename, p.filepath as photo_filepath
                FROM detections d
                JOIN photos p ON d.photo_id = p.id
                ORDER BY d.detected_at DESC
                LIMIT ?
            """, (limit,))
            return [convert_to_serializable(dict(row)) for row in cursor.fetchall()]
    
    # Video methods
    def add_video(self, filename: str, filepath: str, camera_id: int = 0,
                  duration: float = 0.0, trigger_photo_id: Optional[int] = None,
                  filesize: int = 0) -> int:
        """Add a video record."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO videos (filename, filepath, camera_id, duration, trigger_photo_id, filesize)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (filename, filepath, camera_id, duration, trigger_photo_id, filesize))
            conn.commit()
            return cursor.lastrowid
    
    def get_recent_videos(self, limit: int = 20) -> List[Dict]:
        """Get recent videos."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM videos ORDER BY recorded_at DESC LIMIT ?
            """, (limit,))
            return [convert_to_serializable(dict(row)) for row in cursor.fetchall()]
    
    # Statistics methods
    def update_hourly_stats(self, date: str, hour: int, total_photos: int = 0,
                            photos_with_birds: int = 0, total_birds: int = 0,
                            species_counts: Optional[Dict] = None):
        """Update hourly statistics."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO hourly_stats (date, hour, total_photos, photos_with_birds, total_birds, species_counts)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, hour) DO UPDATE SET
                    total_photos = total_photos + excluded.total_photos,
                    photos_with_birds = photos_with_birds + excluded.photos_with_birds,
                    total_birds = total_birds + excluded.total_birds,
                    species_counts = excluded.species_counts
            """, (date, hour, total_photos, photos_with_birds, total_birds,
                  json.dumps(species_counts) if species_counts else None))
            conn.commit()
    
    def get_hourly_heatmap(self, days: int = 7) -> List[Dict]:
        """Get hourly detection heatmap for the last N days."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
            cursor.execute("""
                SELECT date, hour, total_birds, photos_with_birds
                FROM hourly_stats
                WHERE date >= ?
                ORDER BY date, hour
            """, (start_date,))
            return [convert_to_serializable(dict(row)) for row in cursor.fetchall()]
    
    def get_species_stats(self, days: int = 30) -> List[Dict]:
        """Get species detection statistics."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
            cursor.execute("""
                SELECT species, COUNT(*) as count, AVG(confidence) as avg_confidence
                FROM detections
                WHERE detected_at >= ?
                GROUP BY species
                ORDER BY count DESC
            """, (start_date,))
            return [convert_to_serializable(dict(row)) for row in cursor.fetchall()]
    
    def get_daily_summary(self, days: int = 7) -> List[Dict]:
        """Get daily summary statistics."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    DATE(captured_at) as date,
                    COUNT(*) as total_photos,
                    SUM(CASE WHEN has_birds THEN 1 ELSE 0 END) as photos_with_birds,
                    SUM(bird_count) as total_birds
                FROM photos
                WHERE captured_at >= datetime('now', ?)
                GROUP BY DATE(captured_at)
                ORDER BY date DESC
            """, (f'-{days} days',))
            return [convert_to_serializable(dict(row)) for row in cursor.fetchall()]
    
    # System stats methods
    def add_system_stats(self, cpu_percent: float, memory_percent: float,
                        disk_percent: float, temperature: Optional[float] = None,
                        battery_percent: Optional[float] = None,
                        battery_charging: Optional[bool] = None):
        """Add system statistics."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO system_stats 
                (cpu_percent, memory_percent, disk_percent, temperature, battery_percent, battery_charging)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (cpu_percent, memory_percent, disk_percent, temperature,
                  battery_percent, battery_charging))
            conn.commit()
    
    def get_latest_system_stats(self) -> Optional[Dict]:
        """Get latest system statistics."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM system_stats ORDER BY timestamp DESC LIMIT 1
            """)
            row = cursor.fetchone()
            return convert_to_serializable(dict(row)) if row else None
    
    # Cleanup methods
    def cleanup_old_data(self, photo_days: int = 30, video_days: int = 7):
        """Remove old photos and videos."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Get old photo files for deletion
            cursor.execute("""
                SELECT filepath FROM photos 
                WHERE captured_at < datetime('now', ?)
            """, (f'-{photo_days} days',))
            photo_files = [row['filepath'] for row in cursor.fetchall()]
            
            # Get old video files for deletion
            cursor.execute("""
                SELECT filepath FROM videos 
                WHERE recorded_at < datetime('now', ?)
            """, (f'-{video_days} days',))
            video_files = [row['filepath'] for row in cursor.fetchall()]
            
            # Delete old records
            cursor.execute("""
                DELETE FROM photos WHERE captured_at < datetime('now', ?)
            """, (f'-{photo_days} days',))
            
            cursor.execute("""
                DELETE FROM videos WHERE recorded_at < datetime('now', ?)
            """, (f'-{video_days} days',))
            
            conn.commit()
            
            return photo_files, video_files


# Singleton instance
_db_instance = None

def get_database(db_path: str = "data/pirdfy.db") -> Database:
    """Get or create database instance."""
    global _db_instance
    if _db_instance is None:
        _db_instance = Database(db_path)
    return _db_instance
