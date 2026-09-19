"""
dashboard/app.py — Flask Web Dashboard

Provides web UI to manage AutoRepost Pipeline settings, thumbnails, captions, logs.
"""

import os
import sys
import json
from datetime import datetime

# Allow importing from parent directory (for caption.py helpers)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory

# Import config helpers
try:
    from caption import load_config, save_config
except ImportError:
    # Fallback if import fails
    import json as _json
    import tempfile as _tempfile

    CONFIG_PATH_FALLBACK = os.path.join(os.path.dirname(__file__), "..", "config.json")

    def load_config(config_path=CONFIG_PATH_FALLBACK):
        with open(config_path, "r", encoding="utf-8") as f:
            return _json.load(f)

    def save_config(data, config_path=CONFIG_PATH_FALLBACK):
        fd, temp_path = _tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(config_path)))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                _json.dump(data, tmp, indent=2, ensure_ascii=False)
            os.replace(temp_path, config_path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise


# ------------------------------------------------------------------------------
# Flask App Setup
# ------------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = "autorepost-secret-key-change-in-production"  # For flash messages

# Paths
BASE_DIR = os.path.join(os.path.dirname(__file__), "..")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOGS_PATH = os.path.join(BASE_DIR, "logs", "pipeline.log")
THUMBNAILS_DIR = os.path.join(BASE_DIR, "thumbnails")
DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")

# Ensure required directories exist
os.makedirs(THUMBNAILS_DIR, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# Allowed image extensions for thumbnails
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def allowed_file(filename: str) -> bool:
    """Check if file has allowed image extension."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in ALLOWED_EXTENSIONS


def mask_token(token: str) -> str:
    """Mask token for display, showing only last 4 chars."""
    if not token or len(token) < 8:
        return "••••••••"
    return token[:4] + "•" * (len(token) - 8) + token[-4:]


def get_recent_logs(num_lines: int = 20) -> list:
    """Read last N lines from log file."""
    try:
        if not os.path.exists(LOGS_PATH):
            return []
        with open(LOGS_PATH, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            # Get last N lines
            return [line.strip() for line in lines[-num_lines:] if line.strip()]
    except Exception:
        return []


def parse_log_line(line: str) -> dict:
    """
    Parse log line into structured dict for display.
    Expected format: timestamp | url | caption | SUCCESS/FAIL | message
    """
    try:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            return {
                "raw": line,
                "time": parts[0],
                "url": parts[1] if len(parts) > 1 else "N/A",
                "caption": parts[2] if len(parts) > 2 else "N/A",
                "status": parts[3] if len(parts) > 3 else "UNKNOWN",
                "message": parts[4] if len(parts) > 4 else "",
            }
        else:
            # Fallback for non-standard lines
            return {
                "raw": line,
                "time": line[:19] if len(line) >= 19 else "",
                "url": "N/A",
                "caption": line,
                "status": "INFO" if "INFO" in line else "UNKNOWN",
                "message": line,
            }
    except Exception:
        return {
            "raw": line,
            "time": "",
            "url": "N/A",
            "caption": line,
            "status": "UNKNOWN",
            "message": line,
        }


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

@app.route("/")
def index():
    """Dashboard home — show recent activity and stats."""
    try:
        config = load_config()
    except Exception:
        config = {"captions": [], "post_to": [], "default_thumbnail": "Not set"}

    # Stats
    captions_count = len(config.get("captions", []))
    platforms = config.get("post_to", [])
    platforms_count = len(platforms)
    current_thumbnail = config.get("default_thumbnail", "Not set")

    # Recent logs (last 20)
    recent_logs_raw = get_recent_logs(20)
    recent_logs = [parse_log_line(line) for line in reversed(recent_logs_raw)]

    # Pipeline running status — if dashboard runs, assume bot is running
    # In production, you could check via PID file or health endpoint
    pipeline_running = True

    return render_template(
        "index.html",
        captions_count=captions_count,
        platforms_count=platforms_count,
        platforms=platforms,
        current_thumbnail=current_thumbnail,
        recent_logs=recent_logs,
        pipeline_running=pipeline_running,
    )


@app.route("/settings", methods=["GET", "POST"])
def settings():
    """Manage Telegram token and Zernio API key."""
    try:
        config = load_config()
    except FileNotFoundError:
        flash("⚠️ config.json not found! Copy config.example.json to config.json first.", "error")
        config = {
            "telegram_token": "",
            "zernio_api_key": "",
            "default_thumbnail": "thumbnails/default.jpg",
            "post_to": ["instagram", "tiktok"],
            "captions": [],
        }
    except Exception as e:
        flash(f"❌ Error loading config: {e}", "error")
        config = {
            "telegram_token": "",
            "zernio_api_key": "",
            "default_thumbnail": "thumbnails/default.jpg",
            "post_to": ["instagram", "tiktok"],
            "captions": [],
        }

    if request.method == "POST":
        # Update tokens from form
        telegram_token = request.form.get("telegram_token", "").strip()
        zernio_api_key = request.form.get("zernio_api_key", "").strip()

        # Only update if provided (not empty)
        # If masked value submitted, don't overwrite
        if telegram_token and "•" not in telegram_token:
            config["telegram_token"] = telegram_token
        if zernio_api_key and "•" not in zernio_api_key:
            config["zernio_api_key"] = zernio_api_key

        try:
            save_config(config)
            flash("✅ Settings saved!", "success")
        except Exception as e:
            flash(f"❌ Failed to save settings: {e}", "error")

        return redirect(url_for("settings"))

    # GET — mask tokens for display
    telegram_token = config.get("telegram_token", "")
    zernio_api_key = config.get("zernio_api_key", "")

    masked_telegram = mask_token(telegram_token) if telegram_token else ""
    masked_zernio = mask_token(zernio_api_key) if zernio_api_key else ""

    return render_template(
        "settings.html",
        telegram_token=telegram_token,
        zernio_api_key=zernio_api_key,
        masked_telegram=masked_telegram,
        masked_zernio=masked_zernio,
    )


@app.route("/thumbnails")
def thumbnails():
    """List thumbnails and show current default."""
    try:
        config = load_config()
        current_default = config.get("default_thumbnail", "thumbnails/default.jpg")
    except Exception:
        current_default = "thumbnails/default.jpg"

    # List all files in thumbnails dir
    thumbnail_files = []
    try:
        for fname in os.listdir(THUMBNAILS_DIR):
            fpath = os.path.join(THUMBNAILS_DIR, fname)
            if os.path.isfile(fpath):
                ext = os.path.splitext(fname)[1].lower()
                if ext in ALLOWED_EXTENSIONS:
                    # Get file size and modified time
                    try:
                        size = os.path.getsize(fpath)
                        mtime = datetime.fromtimestamp(os.path.getmtime(fpath)).strftime("%Y-%m-%d %H:%M")
                    except:
                        size = 0
                        mtime = "Unknown"
                    thumbnail_files.append(
                        {
                            "filename": fname,
                            "path": f"thumbnails/{fname}",
                            "size": size,
                            "mtime": mtime,
                            "is_default": f"thumbnails/{fname}" == current_default or fname == os.path.basename(current_default),
                        }
                    )
    except Exception as e:
        flash(f"Error listing thumbnails: {e}", "error")

    # Sort by modified time newest first
    thumbnail_files.sort(key=lambda x: x["mtime"], reverse=True)

    return render_template(
        "thumbnails.html",
        thumbnails=thumbnail_files,
        current_default=current_default,
    )


@app.route("/thumbnails/upload", methods=["POST"])
def upload_thumbnail():
    """Handle thumbnail image upload."""
    if "thumbnail" not in request.files:
        flash("❌ No file selected", "error")
        return redirect(url_for("thumbnails"))

    file = request.files["thumbnail"]
    if file.filename == "":
        flash("❌ No file selected", "error")
        return redirect(url_for("thumbnails"))

    if file and allowed_file(file.filename):
        try:
            # Secure filename (basic)
            filename = file.filename
            # Remove path components
            filename = os.path.basename(filename)
            # Replace spaces
            filename = filename.replace(" ", "_")

            save_path = os.path.join(THUMBNAILS_DIR, filename)

            # Avoid overwrite — add suffix if exists
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(save_path):
                filename = f"{base}_{counter}{ext}"
                save_path = os.path.join(THUMBNAILS_DIR, filename)
                counter += 1

            file.save(save_path)
            flash(f"✅ Thumbnail uploaded: {filename}", "success")
        except Exception as e:
            flash(f"❌ Upload failed: {e}", "error")
    else:
        flash("❌ Invalid file type. Only JPG, PNG, WEBP allowed.", "error")

    return redirect(url_for("thumbnails"))


@app.route("/thumbnails/set", methods=["POST"])
def set_thumbnail():
    """Set default thumbnail in config."""
    filename = request.form.get("filename", "").strip()
    if not filename:
        flash("❌ No filename provided", "error")
        return redirect(url_for("thumbnails"))

    # Security: only basename allowed
    filename = os.path.basename(filename)
    thumbnail_path = f"thumbnails/{filename}"
    full_path = os.path.join(THUMBNAILS_DIR, filename)

    if not os.path.exists(full_path):
        flash(f"❌ File not found: {filename}", "error")
        return redirect(url_for("thumbnails"))

    try:
        config = load_config()
        config["default_thumbnail"] = thumbnail_path
        save_config(config)
        flash(f"✅ Default thumbnail updated to {filename}", "success")
    except Exception as e:
        flash(f"❌ Failed to update config: {e}", "error")

    return redirect(url_for("thumbnails"))


@app.route("/thumbnails/file/<path:filename>")
def serve_thumbnail(filename):
    """Serve thumbnail image files."""
    try:
        return send_from_directory(THUMBNAILS_DIR, filename)
    except Exception:
        return "File not found", 404


@app.route("/captions")
def captions():
    """List all captions."""
    try:
        config = load_config()
        captions_list = config.get("captions", [])
    except Exception as e:
        flash(f"Error loading captions: {e}", "error")
        captions_list = []

    # Create list with indexes
    indexed_captions = list(enumerate(captions_list))

    return render_template(
        "captions.html",
        captions=indexed_captions,
        captions_count=len(captions_list),
    )


@app.route("/captions/add", methods=["POST"])
def add_caption():
    """Add new caption to config."""
    new_caption = request.form.get("new_caption", "").strip()

    if not new_caption:
        flash("❌ Caption cannot be empty", "error")
        return redirect(url_for("captions"))

    try:
        config = load_config()
        if "captions" not in config or not isinstance(config["captions"], list):
            config["captions"] = []

        config["captions"].append(new_caption)
        save_config(config)
        flash("✅ Caption added!", "success")
    except Exception as e:
        flash(f"❌ Failed to add caption: {e}", "error")

    return redirect(url_for("captions"))


@app.route("/captions/delete", methods=["POST"])
def delete_caption():
    """Delete caption by index."""
    index_str = request.form.get("index", "").strip()

    try:
        index = int(index_str)
    except ValueError:
        flash("❌ Invalid index", "error")
        return redirect(url_for("captions"))

    try:
        config = load_config()
        captions_list = config.get("captions", [])

        if 0 <= index < len(captions_list):
            removed = captions_list.pop(index)
            config["captions"] = captions_list
            save_config(config)
            flash(f"✅ Caption deleted: {removed[:50]}", "success")
        else:
            flash("❌ Invalid caption index", "error")
    except Exception as e:
        flash(f"❌ Failed to delete caption: {e}", "error")

    return redirect(url_for("captions"))


@app.route("/logs")
def logs():
    """View last 100 log entries."""
    try:
        if os.path.exists(LOGS_PATH):
            with open(LOGS_PATH, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
                # Last 100, reversed so newest first
                last_100 = [line.strip() for line in lines[-100:] if line.strip()]
                last_100_reversed = list(reversed(last_100))
        else:
            last_100_reversed = []
    except Exception as e:
        flash(f"Error reading logs: {e}", "error")
        last_100_reversed = []

    return render_template("logs.html", logs=last_100_reversed)


# ------------------------------------------------------------------------------
# Run App
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    print("🌐 Starting AutoRepost Dashboard on http://0.0.0.0:5000")
    # Ensure templates folder exists
    app.run(host="0.0.0.0", port=5000, debug=False)
