"""
dashboard/app.py — Flask Web Dashboard

Provides web UI to manage AutoRepost Pipeline settings, thumbnails, captions, logs.

Access control
--------------
Every route except /healthz, /login and /logout requires authentication:

* Browsers are redirected to the /login page; a successful login sets an
  HMAC-signed, HttpOnly session cookie.
* Scripts / uptime checkers can send HTTP Basic auth instead
  (``curl -u user:pass http://host:5000/``).

Credentials come from DASHBOARD_USER / DASHBOARD_PASSWORD (set in the
gitignored .env, written by install.sh). If no password is configured the
dashboard fails closed: nobody can log in until one is set.
"""

import os
import sys
import json
import hmac
import time
import base64
import hashlib
import logging
import secrets
from datetime import datetime

# Allow importing from parent directory (for caption.py helpers)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ------------------------------------------------------------------------------
# .env loading (written by install.sh — DASHBOARD_HOST / DASHBOARD_PORT)
# Kept dependency-free: no python-dotenv required.
# ------------------------------------------------------------------------------

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENV_PATH = os.path.join(BASE_DIR, ".env")


def _parse_env_value(raw: str) -> str:
    """
    Turn the right-hand side of a KEY=VALUE line into its value.

    start.sh sources .env with the shell, so install.sh writes anything that is
    not shell-safe (e.g. passwords) single-quoted. Mirror the shell rules here:
    'single quoted' is literal (with '\\'' for an embedded quote), "double
    quoted" honours \\" \\\\ escapes, and bare values end at a " #" comment.
    """
    value = raw.strip()
    if len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("'\\''", "'")
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    for marker in (" #", "\t#"):
        if marker in value:
            value = value.split(marker, 1)[0].rstrip()
    return value


def load_dotenv(path: str = ENV_PATH) -> None:
    """Load KEY=VALUE pairs from .env into os.environ (existing vars win)."""
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                key, _, value = line.partition("=")
                key = key.strip()
                value = _parse_env_value(value)
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception:
        # A broken .env must never take the dashboard down
        pass


load_dotenv()

from flask import (
    Flask,
    g,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    send_from_directory,
)

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

# Paths (BASE_DIR is the project root, resolved above)
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


# ------------------------------------------------------------------------------
# Authentication — configuration
# ------------------------------------------------------------------------------

DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "").strip() or "admin"
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")

AUTH_REALM = "AutoRepost Dashboard"
SESSION_COOKIE_NAME = "autorepost_session"

# Endpoints reachable without credentials. Everything else is protected.
PUBLIC_ENDPOINTS = {"healthz", "login", "logout", "static"}


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _session_ttl_seconds() -> int:
    """Login lifetime; DASHBOARD_SESSION_HOURS in .env, default 12h."""
    try:
        hours = float(os.environ.get("DASHBOARD_SESSION_HOURS", "12"))
    except ValueError:
        hours = 12.0
    if hours <= 0:
        hours = 12.0
    return int(hours * 3600)


SESSION_TTL_SECONDS = _session_ttl_seconds()

# Only honour X-Forwarded-* headers when explicitly told the app sits behind a
# trusted reverse proxy — otherwise clients could spoof their IP in the logs.
TRUST_PROXY = _env_flag("DASHBOARD_TRUST_PROXY")
FORCE_SECURE_COOKIE = _env_flag("DASHBOARD_COOKIE_SECURE")


def _resolve_secret_key() -> bytes:
    """
    Key used to sign session cookies (and Flask's flash messages).

    Priority: DASHBOARD_SECRET_KEY, then FLASK_SECRET_KEY (install.sh writes
    the former to .env). Without either, a random per-process key is used —
    everything still works, users just have to log in again after a restart.
    The old hard-coded fallback would have let anyone forge a session cookie.
    """
    for name in ("DASHBOARD_SECRET_KEY", "FLASK_SECRET_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value.encode("utf-8")
    return secrets.token_bytes(32)


SECRET_KEY = _resolve_secret_key()
app.secret_key = SECRET_KEY

# Sessions are signed with a key derived from the secret *and* the current
# credentials, so changing the password invalidates every existing login.
_SESSION_SIGNING_KEY = hmac.new(
    SECRET_KEY,
    b"autorepost-dashboard-session\0"
    + hashlib.sha256(f"{DASHBOARD_USER}\0{DASHBOARD_PASSWORD}".encode("utf-8")).digest(),
    hashlib.sha256,
).digest()


# ------------------------------------------------------------------------------
# Authentication — audit log (shares logs/pipeline.log with the bot)
# ------------------------------------------------------------------------------

auth_logger = logging.getLogger("AutoRepost.dashboard")
auth_logger.setLevel(logging.INFO)
auth_logger.propagate = False
if not auth_logger.handlers:
    _fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    try:
        _file_handler = logging.FileHandler(LOGS_PATH, encoding="utf-8")
        _file_handler.setFormatter(_fmt)
        auth_logger.addHandler(_file_handler)
    except Exception:
        # An unwritable log file must not take the dashboard down
        pass
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_fmt)
    auth_logger.addHandler(_console_handler)


def auth_configured() -> bool:
    """True when a dashboard password has been set (auth fails closed otherwise)."""
    return bool(DASHBOARD_PASSWORD)


def client_ip() -> str:
    """Best-effort client address for log lines."""
    if TRUST_PROXY:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip() or (request.remote_addr or "unknown")
    return request.remote_addr or "unknown"


def _sanitize_for_log(value: str, limit: int = 64) -> str:
    """Keep user-supplied strings from injecting fake log records."""
    cleaned = "".join(ch for ch in (value or "") if ch.isprintable()).replace("|", "/")
    return cleaned[:limit]


def log_login_attempt(username: str, success: bool, method: str) -> None:
    """Record every login attempt (success or failure) in logs/pipeline.log."""
    outcome = "OK" if success else "FAILED"
    message = (
        f"Dashboard login {outcome} | user '{_sanitize_for_log(username)}' "
        f"from {client_ip()} via {method}"
    )
    if success:
        auth_logger.info(message)
    else:
        auth_logger.warning(message)


# ------------------------------------------------------------------------------
# Authentication — credential + session checks
# ------------------------------------------------------------------------------

def credentials_valid(username: str, password: str) -> bool:
    """Constant-time comparison of a username/password pair against .env."""
    if not auth_configured():
        return False
    user_ok = hmac.compare_digest((username or "").encode("utf-8"), DASHBOARD_USER.encode("utf-8"))
    pass_ok = hmac.compare_digest((password or "").encode("utf-8"), DASHBOARD_PASSWORD.encode("utf-8"))
    return user_ok and pass_ok


def basic_auth_credentials():
    """Return (username, password) from an ``Authorization: Basic`` header, or None."""
    header = request.headers.get("Authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded.strip():
        return None
    try:
        decoded = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
    except Exception:
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password


def make_session_token() -> str:
    """``<expiry>.<hmac>`` — nothing secret inside, only a signed expiry."""
    expires = int(time.time()) + SESSION_TTL_SECONDS
    signature = hmac.new(_SESSION_SIGNING_KEY, str(expires).encode("ascii"), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def verify_session_token(token) -> bool:
    if not token or not isinstance(token, str) or "." not in token:
        return False
    expires_str, _, signature = token.partition(".")
    if not expires_str.isdigit() or not signature:
        return False
    expected = hmac.new(_SESSION_SIGNING_KEY, expires_str.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False
    return int(expires_str) > time.time()


def request_is_secure() -> bool:
    if FORCE_SECURE_COOKIE or request.is_secure:
        return True
    if TRUST_PROXY:
        return request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"
    return False


def set_session_cookie(response):
    response.set_cookie(
        SESSION_COOKIE_NAME,
        make_session_token(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=request_is_secure(),
        path="/",
    )
    return response


def clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


def is_authenticated() -> bool:
    """Valid session cookie *or* valid Basic credentials on this request."""
    if getattr(g, "auth_checked", False):
        return bool(g.authenticated)
    g.auth_checked = True
    g.authenticated = False
    g.auth_method = None

    if verify_session_token(request.cookies.get(SESSION_COOKIE_NAME)):
        g.authenticated, g.auth_method = True, "session"
        return True

    creds = basic_auth_credentials()
    if creds is not None:
        if credentials_valid(*creds):
            g.authenticated, g.auth_method = True, "basic"
            return True
        # Wrong Basic credentials are a failed login too
        log_login_attempt(creds[0], success=False, method="basic")
        g.auth_method = "basic-failed"
    return False


def wants_html() -> bool:
    """Browsers navigating to a page get the login form; API clients get a 401."""
    return "text/html" in request.headers.get("Accept", "")


def unauthorized_response():
    body = json.dumps({"error": "unauthorized", "login": "/login"})
    return (
        body,
        401,
        {
            "Content-Type": "application/json",
            "WWW-Authenticate": f'Basic realm="{AUTH_REALM}", charset="UTF-8"',
        },
    )


def safe_next_url(candidate) -> str:
    """Only allow same-site relative paths in ?next= (no open redirects)."""
    if not candidate or not isinstance(candidate, str):
        return url_for("index")
    if not candidate.startswith("/") or candidate.startswith("//") or candidate.startswith("/\\"):
        return url_for("index")
    if any(ch.isspace() or not ch.isprintable() for ch in candidate):
        return url_for("index")
    if candidate.split("?", 1)[0].rstrip("/") in {"/login", "/logout"}:
        return url_for("index")
    return candidate


@app.before_request
def require_dashboard_auth():
    """Gate every request that is not explicitly public."""
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if is_authenticated():
        return None
    # Basic credentials were sent but rejected: let the client retry.
    if getattr(g, "auth_method", None) == "basic-failed":
        return unauthorized_response()
    if wants_html() and request.method == "GET":
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("login", next=next_url.rstrip("?")))
    return unauthorized_response()


@app.after_request
def no_store_for_private_pages(response):
    """Authenticated pages must not linger in shared/browser caches after logout."""
    if request.endpoint not in PUBLIC_ENDPOINTS and request.endpoint != "serve_thumbnail":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.context_processor
def inject_auth_context():
    return {
        "auth_user": DASHBOARD_USER,
        "auth_method": getattr(g, "auth_method", None),
    }


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


def bot_is_running() -> bool:
    """
    Best-effort check that the Telegram bot process is alive.

    1. Prefer logs/bot.pid (written by bot.py on startup).
    2. Fall back to scanning /proc for a process running bot.py (Linux only).
    """
    pid_file = os.path.join(BASE_DIR, "logs", "bot.pid")

    try:
        if os.path.exists(pid_file):
            with open(pid_file, "r", encoding="utf-8") as fh:
                pid = int(fh.read().strip() or 0)
            if pid > 0:
                os.kill(pid, 0)  # raises OSError when the process is gone
                return True
    except Exception:
        pass

    # Fallback: look for a live `python bot.py` process
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", "rb") as fh:
                    cmdline = fh.read().decode("utf-8", "ignore")
            except Exception:
                continue
            if "bot.py" in cmdline and "dashboard" not in cmdline:
                return True
    except Exception:
        pass

    return False


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

    # Pipeline status: the dashboard and the bot are separate processes, so probe
    # the bot's health file (written by bot.py) before claiming it is running.
    pipeline_running = bot_is_running()

    return render_template(
        "index.html",
        captions_count=captions_count,
        platforms_count=platforms_count,
        platforms=platforms,
        current_thumbnail=current_thumbnail,
        recent_logs=recent_logs,
        pipeline_running=pipeline_running,
    )


@app.route("/healthz")
def healthz():
    """Public liveness endpoint with a deliberately minimal response."""
    # Keep this contract stable: health monitors should not receive hostnames,
    # token state, log counts, or any other operational information.
    return json.dumps({"status": "ok", "auth_required": True}), 200, {"Content-Type": "application/json"}


@app.route("/login", methods=["GET", "POST"])
def login():
    """Login form; a correct password sets the signed session cookie."""
    next_url = safe_next_url(request.values.get("next"))

    if request.method == "GET" and is_authenticated():
        return redirect(next_url)

    error = None
    status = 200
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if credentials_valid(username, password):
            log_login_attempt(username, success=True, method="form")
            return set_session_cookie(redirect(next_url))

        log_login_attempt(username, success=False, method="form")
        error = "Invalid username or password."
        status = 401

    return (
        render_template(
            "login.html",
            error=error,
            next_url=next_url,
            auth_configured=auth_configured(),
            username=request.form.get("username", "") if request.method == "POST" else "",
        ),
        status,
    )


@app.route("/logout", methods=["POST"])
def logout():
    """Clear the session cookie (Basic-auth clients simply stop sending the header)."""
    flash("👋 You have been logged out.", "info")
    return clear_session_cookie(redirect(url_for("login")))


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
    HOST = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    try:
        PORT = int(os.environ.get("DASHBOARD_PORT", "5000"))
    except ValueError:
        PORT = 5000

    # Ensure templates folder exists
    os.makedirs(os.path.join(os.path.dirname(__file__), "templates"), exist_ok=True)

    print(f"🌐 Starting AutoRepost Dashboard on http://{HOST}:{PORT}")
    if auth_configured():
        print(f"🔒 Dashboard login enabled (user: {DASHBOARD_USER})")
    else:
        print(
            "⚠️  DASHBOARD_PASSWORD is not set — nobody can log in. "
            "Add DASHBOARD_USER / DASHBOARD_PASSWORD to .env (or re-run ./install.sh) and restart."
        )
    if not os.environ.get("DASHBOARD_SECRET_KEY") and not os.environ.get("FLASK_SECRET_KEY"):
        print("ℹ️  DASHBOARD_SECRET_KEY not set — logins will not survive a restart.")
    app.run(host=HOST, port=PORT, debug=False)
