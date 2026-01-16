"""
Pirdfy Web Application
Flask-based web server with REST API and WebSocket support.
"""

import os
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from flask import Flask, render_template, jsonify, request, send_from_directory, Response
from flask_socketio import SocketIO, emit
from flask_cors import CORS

logger = logging.getLogger(__name__)


def create_app(config: dict, camera_manager=None, detector=None, pipeline=None,
               video_recorder=None, system_monitor=None, database=None):
    """
    Create and configure the Flask application.
    """
    # Determine template and static paths
    web_dir = Path(__file__).parent
    template_dir = web_dir / "templates"
    static_dir = web_dir / "static"
    
    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir)
    )
    
    # Configuration
    web_config = config.get("web", {})
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "pirdfy-secret-key-change-me")
    app.config["DEBUG"] = web_config.get("debug", False)
    
    # Enable CORS
    CORS(app)
    
    # Initialize SocketIO
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")
    
    # Store references
    app.camera_manager = camera_manager
    app.detector = detector
    app.pipeline = pipeline
    app.video_recorder = video_recorder
    app.system_monitor = system_monitor
    app.database = database
    app.config_data = config
    
    # Data paths - resolve to absolute for send_from_directory
    data_path = Path(config.get("storage", {}).get("data_path", "data")).resolve()
    photos_path = data_path / "photos"
    birds_path = data_path / "birds"
    videos_path = data_path / "videos"
    annotated_path = data_path / "annotated"
    
    # Ensure directories exist
    for p in [photos_path, birds_path, videos_path, annotated_path]:
        p.mkdir(parents=True, exist_ok=True)
    
    # ================== Page Routes ==================
    
    @app.route("/")
    def index():
        """Main dashboard page."""
        return render_template("index.html")
    
    @app.route("/gallery")
    def gallery():
        """Photo gallery page."""
        return render_template("gallery.html")
    
    @app.route("/stats")
    def stats():
        """Statistics page."""
        return render_template("stats.html")
    
    @app.route("/settings")
    def settings():
        """Settings page."""
        return render_template("settings.html")
    
    # ================== API Routes ==================
    
    # --- Photos ---
    @app.route("/api/photos")
    def api_get_photos():
        """Get recent photos."""
        limit = request.args.get("limit", 50, type=int)
        birds_only = request.args.get("birds_only", "false").lower() == "true"
        camera_id = request.args.get("camera_id", None, type=int)
        
        photos = database.get_recent_photos(
            limit=limit,
            with_birds_only=birds_only,
            camera_id=camera_id
        )
        
        return jsonify({
            "success": True,
            "photos": photos,
            "count": len(photos)
        })
    
    @app.route("/api/photos/<int:photo_id>")
    def api_get_photo(photo_id: int):
        """Get photo details."""
        photo = database.get_photo(photo_id)
        if photo:
            detections = database.get_detections_for_photo(photo_id)
            return jsonify({
                "success": True,
                "photo": photo,
                "detections": detections
            })
        return jsonify({"success": False, "error": "Photo not found"}), 404
    
    @app.route("/api/photos/image/<path:filename>")
    def api_get_photo_image(filename: str):
        """Serve a photo image."""
        return send_from_directory(str(photos_path), filename)
    
    @app.route("/api/photos/annotated/<path:filename>")
    def api_get_annotated_image(filename: str):
        """Serve an annotated photo image."""
        return send_from_directory(str(annotated_path), filename)
    
    # --- Birds ---
    @app.route("/api/birds")
    def api_get_birds():
        """Get recent bird detections."""
        limit = request.args.get("limit", 50, type=int)
        detections = database.get_recent_detections(limit=limit)
        return jsonify({
            "success": True,
            "detections": detections,
            "count": len(detections)
        })
    
    @app.route("/api/birds/image/<path:filename>")
    def api_get_bird_image(filename: str):
        """Serve a cropped bird image."""
        return send_from_directory(str(birds_path), filename)
    
    # --- Videos ---
    @app.route("/api/videos")
    def api_get_videos():
        """Get recent videos."""
        limit = request.args.get("limit", 20, type=int)
        videos = database.get_recent_videos(limit=limit)
        return jsonify({
            "success": True,
            "videos": videos,
            "count": len(videos)
        })
    
    @app.route("/api/videos/file/<path:filename>")
    def api_get_video_file(filename: str):
        """Serve a video file."""
        return send_from_directory(str(videos_path), filename)
    
    # --- Statistics ---
    @app.route("/api/stats/hourly")
    def api_get_hourly_stats():
        """Get hourly detection heatmap data."""
        days = request.args.get("days", 7, type=int)
        heatmap = database.get_hourly_heatmap(days=days)
        return jsonify({
            "success": True,
            "heatmap": heatmap,
            "days": days
        })
    
    @app.route("/api/stats/species")
    def api_get_species_stats():
        """Get species detection statistics."""
        days = request.args.get("days", 30, type=int)
        species = database.get_species_stats(days=days)
        return jsonify({
            "success": True,
            "species": species,
            "days": days
        })
    
    @app.route("/api/stats/daily")
    def api_get_daily_stats():
        """Get daily summary statistics."""
        days = request.args.get("days", 7, type=int)
        summary = database.get_daily_summary(days=days)
        return jsonify({
            "success": True,
            "summary": summary,
            "days": days
        })
    
    @app.route("/api/stats/pipeline")
    def api_get_pipeline_stats():
        """Get detection pipeline statistics."""
        if pipeline:
            return jsonify({
                "success": True,
                "stats": pipeline.get_stats()
            })
        return jsonify({"success": False, "error": "Pipeline not available"})
    
    # --- Configuration ---
    @app.route("/api/config", methods=["GET"])
    def api_get_config():
        """Get current configuration."""
        return jsonify({
            "success": True,
            "config": {
                "camera": {
                    "capture_interval": camera_manager.get_capture_interval() if camera_manager else 1,
                    "cameras": camera_manager.get_camera_info() if camera_manager else []
                },
                "detection": detector.get_model_info() if detector else {},
                "video": video_recorder.get_config() if video_recorder else {},
                "web": web_config
            }
        })
    
    @app.route("/api/config", methods=["POST"])
    def api_update_config():
        """Update configuration."""
        data = request.get_json()
        
        try:
            # Update capture interval
            if "capture_interval" in data and camera_manager:
                camera_manager.set_capture_interval(data["capture_interval"])
            
            # Update confidence threshold
            if "confidence_threshold" in data and detector:
                detector.set_confidence_threshold(data["confidence_threshold"])
            
            # Update video settings
            if video_recorder:
                if "video_enabled" in data:
                    video_recorder.set_enabled(data["video_enabled"])
                if "video_duration" in data:
                    video_recorder.set_duration(data["video_duration"])
                if "video_cooldown" in data:
                    video_recorder.set_cooldown(data["video_cooldown"])
            
            return jsonify({"success": True})
            
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 400
    
    # --- Camera Settings ---
    @app.route("/api/camera/<int:camera_id>/settings", methods=["GET"])
    def api_get_camera_settings(camera_id: int):
        """Get camera settings."""
        if camera_manager:
            cameras = camera_manager.get_camera_info()
            for cam in cameras:
                if cam["id"] == camera_id:
                    return jsonify({"success": True, "settings": cam})
        return jsonify({"success": False, "error": "Camera not found"}), 404
    
    @app.route("/api/camera/<int:camera_id>/settings", methods=["POST"])
    def api_update_camera_settings(camera_id: int):
        """Update camera settings."""
        data = request.get_json()
        
        if camera_manager:
            success = camera_manager.update_camera_settings(camera_id, **data)
            if success:
                return jsonify({"success": True})
        
        return jsonify({"success": False, "error": "Failed to update settings"}), 400
    
    @app.route("/api/camera/<int:camera_id>/capture", methods=["POST"])
    def api_trigger_capture(camera_id: int):
        """Trigger a single capture."""
        if camera_manager:
            result = camera_manager.capture_single(camera_id)
            if result.success:
                # Process through pipeline
                if pipeline:
                    detection_result = pipeline.process_capture(result)
                    return jsonify({
                        "success": True,
                        "photo": {
                            "filename": result.filename,
                            "filepath": result.filepath,
                            "has_birds": len(detection_result.detections) > 0 if detection_result else False,
                            "bird_count": len(detection_result.detections) if detection_result else 0
                        }
                    })
                return jsonify({
                    "success": True,
                    "photo": {
                        "filename": result.filename,
                        "filepath": result.filepath
                    }
                })
        return jsonify({"success": False, "error": "Camera not available"}), 400
    
    # --- System Status ---
    @app.route("/api/status")
    def api_get_status():
        """Get system status."""
        status_data = {
            "success": True,
            "timestamp": datetime.now().isoformat(),
            "system": system_monitor.get_status_dict() if system_monitor else None,
            "uptime": system_monitor.get_uptime_formatted() if system_monitor else None,
            "cameras": {
                "count": len(camera_manager.cameras) if camera_manager else 0,
                "capturing": camera_manager._running if camera_manager else False
            },
            "video": video_recorder.get_status() if video_recorder else None,
            "pipeline": pipeline.get_stats() if pipeline else None
        }
        return jsonify(status_data)
    
    @app.route("/api/status/battery")
    def api_get_battery():
        """Get battery status."""
        if system_monitor:
            status = system_monitor.get_status_dict()
            return jsonify({
                "success": True,
                "battery": status.get("battery")
            })
        return jsonify({"success": False, "error": "System monitor not available"})
    
    # --- Control ---
    @app.route("/api/control/start", methods=["POST"])
    def api_start_capture():
        """Start continuous capture."""
        if camera_manager:
            camera_manager.start_continuous_capture()
            return jsonify({"success": True, "message": "Capture started"})
        return jsonify({"success": False, "error": "Camera not available"}), 400
    
    @app.route("/api/control/stop", methods=["POST"])
    def api_stop_capture():
        """Stop continuous capture."""
        if camera_manager:
            camera_manager.stop_continuous_capture()
            return jsonify({"success": True, "message": "Capture stopped"})
        return jsonify({"success": False, "error": "Camera not available"}), 400
    
    # ================== WebSocket Events ==================
    
    @socketio.on("connect")
    def handle_connect():
        """Handle client connection."""
        logger.info("WebSocket client connected")
        emit("connected", {"message": "Connected to Pirdfy"})
    
    @socketio.on("disconnect")
    def handle_disconnect():
        """Handle client disconnection."""
        logger.info("WebSocket client disconnected")
    
    @socketio.on("subscribe")
    def handle_subscribe(data):
        """Handle subscription to updates."""
        channel = data.get("channel", "all")
        logger.info(f"Client subscribed to {channel}")
        emit("subscribed", {"channel": channel})
    
    # Function to emit events (called from other modules)
    def emit_new_photo(photo_data):
        """Emit new photo event to all clients."""
        socketio.emit("new_photo", photo_data)
    
    def emit_bird_detected(detection_data):
        """Emit bird detection event to all clients."""
        socketio.emit("bird_detected", detection_data)
    
    def emit_recording_started(recording_data):
        """Emit recording started event."""
        socketio.emit("recording_started", recording_data)
    
    def emit_recording_ended(recording_data):
        """Emit recording ended event."""
        socketio.emit("recording_ended", recording_data)
    
    def emit_status_update(status_data):
        """Emit status update event."""
        socketio.emit("status_update", status_data)
    
    # Store emit functions on app for access from main.py
    app.emit_new_photo = emit_new_photo
    app.emit_bird_detected = emit_bird_detected
    app.emit_recording_started = emit_recording_started
    app.emit_recording_ended = emit_recording_ended
    app.emit_status_update = emit_status_update
    app.socketio = socketio
    
    return app, socketio


def run_server(app, socketio, host: str = "0.0.0.0", port: int = 8080):
    """Run the web server."""
    logger.info(f"Starting web server on {host}:{port}")
    socketio.run(app, host=host, port=port, debug=app.config["DEBUG"])
