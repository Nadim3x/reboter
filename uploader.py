"""
uploader.py — Zernio API Uploader

Publishes a downloaded video to Instagram, TikTok (and optionally Facebook /
YouTube) through the Zernio API, following the flow documented at
https://docs.zernio.com:

    1. GET  /v1/accounts          -> id of the connected account per platform
    2. POST /v1/media/presign     -> presigned storage URL for the video file
    3. PUT  <uploadUrl>           -> upload the bytes (no Authorization header)
    4. POST /v1/posts             -> publish to every platform in one request
                                     (publishNow: true)
    5. GET  /v1/posts/{postId}    -> only when a platform is still "processing"
                                     when step 4 answers; polled until the post
                                     reaches a terminal state

There is no ``/v1/upload`` endpoint on Zernio: the previous implementation
posted a multipart form there and every upload failed with
``404 endpoint_not_found``.

Base URL: ``https://zernio.com/api/v1``. Legacy values that older versions of
this project wrote to config.json (``https://api.zernio.com``, ``.../upload``)
are normalised automatically, see :func:`normalize_api_base_url`.

Thumbnails / covers
-------------------
A published ``publishNow`` request can stay "processing" for a while on some
platforms, and every platform takes its cover image somewhere else:

    Instagram Reels   platforms[].platformSpecificData.instagramThumbnail
    TikTok            tiktokSettings.video_cover_image_url
    Facebook / YouTube / LinkedIn
                      mediaItems[].thumbnail

``mediaItems[].thumbnail`` is ignored by Instagram and TikTok, which is why the
old code posted "without a thumbnail" on exactly those two platforms. When
``use_thumbnail`` is true (the default) the configured ``default_thumbnail`` is
uploaded once and wired into every cover field the target platforms support.

Configuration keys read from config.json:

    zernio_api_key        "sk_..." from https://zernio.com/dashboard/api-keys
    zernio_api_base_url   defaults to https://zernio.com/api/v1
    post_to               ["instagram", "tiktok"]
    zernio_account_ids    optional {"instagram": "<24-char id>", ...} overrides;
                          otherwise the first active account per platform is used
    default_thumbnail     image used as the post cover (e.g. thumbnails/default.jpg)
    use_thumbnail         false -> post without a custom cover (default: true)
    tiktok_settings       merged over DEFAULT_TIKTOK_SETTINGS (TikTok requires
                          privacy_level + the two consent flags on every post)
    instagram_settings    optional platformSpecificData for Instagram
                          (e.g. {"shareToFeed": false}); an instagramThumbnail /
                          reelCover set here wins over default_thumbnail
    post_status_wait_seconds
                          how long to poll GET /v1/posts/{postId} while a
                          platform is still processing (default 120, 0 = don't)
    post_status_poll_interval
                          seconds between those polls (default 5)
"""

import os
import json
import time
import uuid
import struct
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import requests

from caption import load_config

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------------------

DEFAULT_ZERNIO_API_BASE_URL = "https://zernio.com/api/v1"

# Hosts / paths older versions of this project wrote to config.json. They are
# remapped to the documented base URL so existing installs keep working.
LEGACY_ZERNIO_HOSTS = {"api.zernio.com", "www.api.zernio.com"}
ZERNIO_HOSTS = {"zernio.com", "www.zernio.com"}

SUPPORTED_PLATFORMS = ("instagram", "tiktok", "facebook", "youtube")

# Display names for user-facing messages.
PLATFORM_NAMES = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "facebook": "Facebook",
    "youtube": "YouTube",
    "linkedin": "LinkedIn",
}

# ``mediaItems[].thumbnail`` is only honoured by these platforms (docs: Media
# Uploads guide). Instagram and TikTok take a cover of their own instead:
# Instagram via platformSpecificData.instagramThumbnail, TikTok via
# tiktokSettings.video_cover_image_url.
THUMBNAIL_PLATFORMS = {"facebook", "youtube", "linkedin"}
INSTAGRAM_COVER_EXTENSIONS = {".jpg", ".jpeg", ".png"}
TIKTOK_COVER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png"}

TIKTOK_COVER_MAX_BYTES = 20 * 1024 * 1024      # docs: at most 20 MB
MEDIA_THUMBNAIL_MAX_BYTES = 10 * 1024 * 1024   # docs: JPG/PNG, 10 MB max

# Zernio's per-platform statuses (docs: post lifecycle). "processing" and
# friends are transient: the platform target has not finished yet and the real
# outcome arrives via GET /v1/posts/{postId} (and the post webhooks).
PUBLISHED_PLATFORM_STATUSES = {"published"}
PENDING_PLATFORM_STATUSES = {"pending", "processing", "uploading", "publishing", "scheduled", "queued"}

# publishNow answers with terminal results, but Instagram Reels (and, for large
# videos, TikTok) can still be processing at that point. Poll instead of
# reporting a half-finished post as a failure.
DEFAULT_STATUS_WAIT_SECONDS = 120
DEFAULT_STATUS_POLL_INTERVAL = 5.0
MAX_STATUS_WAIT_SECONDS = 900

# TikTok rejects a post that lacks privacy_level and the two consent flags.
DEFAULT_TIKTOK_SETTINGS: Dict[str, Any] = {
    "privacy_level": "PUBLIC_TO_EVERYONE",
    "allow_comment": True,
    "allow_duet": True,
    "allow_stitch": True,
    "content_preview_confirmed": True,
    "express_consent_given": True,
}

# MIME types accepted by POST /v1/media/presign (generic types are rejected).
VIDEO_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4v": "video/x-m4v",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
}
IMAGE_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

API_TIMEOUT = 30          # seconds, JSON API calls
UPLOAD_TIMEOUT = 600      # seconds, PUT of the video to storage (up to 100 MB)
PUBLISH_TIMEOUT = 300     # seconds, publishNow waits for every platform

PLACEHOLDER_API_KEYS = {"", "YOUR_ZERNIO_API_KEY", "your_zernio_api_key_here"}

# Per-platform cover capabilities: which image extensions are accepted, the
# maximum file size, and the field the URL ends up in.
COVER_RULES: Dict[str, Dict[str, Any]] = {
    "instagram": {"extensions": INSTAGRAM_COVER_EXTENSIONS, "max_bytes": None, "field": "platformSpecificData.instagramThumbnail"},
    "tiktok": {"extensions": TIKTOK_COVER_EXTENSIONS, "max_bytes": TIKTOK_COVER_MAX_BYTES, "field": "tiktokSettings.video_cover_image_url"},
    "facebook": {"extensions": MEDIA_THUMBNAIL_EXTENSIONS, "max_bytes": MEDIA_THUMBNAIL_MAX_BYTES, "field": "mediaItems[].thumbnail"},
    "youtube": {"extensions": MEDIA_THUMBNAIL_EXTENSIONS, "max_bytes": MEDIA_THUMBNAIL_MAX_BYTES, "field": "mediaItems[].thumbnail"},
    "linkedin": {"extensions": MEDIA_THUMBNAIL_EXTENSIONS, "max_bytes": MEDIA_THUMBNAIL_MAX_BYTES, "field": "mediaItems[].thumbnail"},
}


# ------------------------------------------------------------------------------
# Errors
# ------------------------------------------------------------------------------

class ZernioError(Exception):
    """A non-2xx answer (or transport failure) from the Zernio API."""

    def __init__(
        self,
        message: str,
        status: Optional[int] = None,
        code: Optional[str] = None,
        error_type: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.error_type = error_type
        self.payload = payload or {}

    def __str__(self) -> str:
        parts = []
        if self.status:
            parts.append(f"HTTP {self.status}")
        if self.code:
            parts.append(self.code)
        prefix = f"[{' '.join(parts)}] " if parts else ""
        return f"{prefix}{self.message}"


# ------------------------------------------------------------------------------
# Base URL handling
# ------------------------------------------------------------------------------

def normalize_api_base_url(base_url: Optional[str]) -> str:
    """Validate the configured base URL and return the versioned API root.

    Accepted inputs and their results::

        None / ""                          -> https://zernio.com/api/v1
        https://zernio.com/api/v1          -> https://zernio.com/api/v1
        https://zernio.com/api             -> https://zernio.com/api/v1
        https://zernio.com                 -> https://zernio.com/api/v1
        https://api.zernio.com  (legacy)   -> https://zernio.com/api/v1
        https://api.zernio.com/v1/upload   -> https://zernio.com/api/v1
        https://proxy.example.com          -> https://proxy.example.com/v1
        https://proxy.example.com/v1       -> https://proxy.example.com/v1

    Raises ``ValueError`` with a user-facing reason for malformed values.
    """
    value = base_url.strip() if isinstance(base_url, str) else ""
    if not value:
        return DEFAULT_ZERNIO_API_BASE_URL

    if any(character.isspace() for character in value):
        raise ValueError("the URL must not contain spaces")

    try:
        parsed = urlsplit(value)
        _ = parsed.port  # raises for malformed ports such as ":abc"
    except ValueError as exc:
        raise ValueError(f"invalid URL ({exc})") from exc

    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("use a complete http:// or https:// URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs must not contain embedded credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("do not include a query string or fragment")

    host = (parsed.hostname or "").lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")

    # Legacy: the fictional api.zernio.com host from earlier releases.
    if host in LEGACY_ZERNIO_HOSTS:
        return DEFAULT_ZERNIO_API_BASE_URL

    # Legacy: a "complete upload URL" ending in /upload.
    if path.endswith("/upload"):
        path = path[: -len("/upload")]

    if host in ZERNIO_HOSTS:
        # Only https://zernio.com/api/v1 exists (covers "", "/api" and "/api/v1").
        return DEFAULT_ZERNIO_API_BASE_URL

    # Custom host (reverse proxy, mock server, future region): make sure the
    # version segment is present exactly once.
    if not path.endswith("/v1"):
        path = f"{path}/v1"
    return f"{scheme}://{netloc}{path}"


# Backwards-compatible alias: older code/tests imported this name. It now
# returns the API root (there is no upload endpoint to build).
def build_upload_url(base_url: Optional[str]) -> str:  # pragma: no cover - alias
    return normalize_api_base_url(base_url)


def api_url(base_url: str, path: str) -> str:
    """Join the normalised API root with an endpoint path such as ``/posts``."""
    if not path.startswith("/"):
        path = "/" + path
    return f"{base_url.rstrip('/')}{path}"


# ------------------------------------------------------------------------------
# Low level HTTP
# ------------------------------------------------------------------------------

def _auth_headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def _parse_json(response: requests.Response) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"data": data}


def _error_from_response(response: requests.Response, what: str) -> ZernioError:
    payload = _parse_json(response)
    message = payload.get("error") or payload.get("message") or response.text[:300] or response.reason
    code = payload.get("code")
    error_type = payload.get("type")
    hint = ""
    if response.status_code == 401:
        hint = " — check the Zernio API key in Settings (it starts with sk_)"
    elif response.status_code == 404 and code == "endpoint_not_found":
        hint = " — check the Zernio API base URL in Settings (should be https://zernio.com/api/v1)"
    elif response.status_code == 402:
        hint = " — Zernio billing gate: add a payment method / upgrade the plan at zernio.com"
    elif response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        hint = f" — rate limited, retry after {retry_after}s" if retry_after else " — rate limited, retry later"
    if payload.get("platformError"):
        try:
            raw = json.dumps(payload["platformError"])[:300]
        except Exception:
            raw = str(payload["platformError"])[:300]
        hint += f" (platform said: {raw})"
    return ZernioError(f"{what}: {message}{hint}", response.status_code, code, error_type, payload)


def _api_request(method: str, url: str, api_key: str, what: str, **kwargs) -> Tuple[int, Dict[str, Any]]:
    """Perform an authenticated JSON request; raise ZernioError on failure."""
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update(_auth_headers(api_key))
    timeout = kwargs.pop("timeout", API_TIMEOUT)
    try:
        response = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except requests.exceptions.Timeout as exc:
        raise ZernioError(f"{what}: request timed out after {timeout}s") from exc
    except requests.exceptions.ConnectionError as exc:
        raise ZernioError(f"{what}: connection error ({str(exc)[:120]})") from exc
    except requests.exceptions.RequestException as exc:
        raise ZernioError(f"{what}: request failed ({str(exc)[:120]})") from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise _error_from_response(response, what)
    return response.status_code, _parse_json(response)


# ------------------------------------------------------------------------------
# Zernio API calls
# ------------------------------------------------------------------------------

def list_accounts(api_key: str, base_url: Optional[str] = None) -> List[Dict[str, Any]]:
    """``GET /v1/accounts`` — every social account connected to the Zernio team."""
    root = normalize_api_base_url(base_url)
    _, data = _api_request("GET", api_url(root, "/accounts"), api_key, "listing Zernio accounts")
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        raise ZernioError("listing Zernio accounts: unexpected response (no 'accounts' array)", payload=data)
    return [a for a in accounts if isinstance(a, dict)]


def resolve_account_ids(
    platforms: List[str],
    config: Dict[str, Any],
    api_key: str,
    base_url: Optional[str] = None,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Map each requested platform to a Zernio ``accountId``.

    Explicit ids from ``config["zernio_account_ids"]`` win; anything else is
    looked up once via ``GET /v1/accounts`` (first *active* account of that
    platform). Returns ``(resolved, errors)`` keyed by platform.
    """
    overrides = config.get("zernio_account_ids") or {}
    if not isinstance(overrides, dict):
        overrides = {}

    resolved: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    missing: List[str] = []

    for platform in platforms:
        override = overrides.get(platform)
        if isinstance(override, str) and override.strip():
            resolved[platform] = override.strip()
        else:
            missing.append(platform)

    if not missing:
        return resolved, errors

    try:
        accounts = list_accounts(api_key, base_url)
    except ZernioError as exc:
        for platform in missing:
            errors[platform] = f"failed: {exc}"
        return resolved, errors

    for platform in missing:
        candidates = [a for a in accounts if str(a.get("platform", "")).lower() == platform]
        active = [a for a in candidates if a.get("isActive", True)]
        chosen = (active or candidates)[:1]
        if not chosen:
            errors[platform] = (
                f"failed: no {platform} account is connected to Zernio — connect one at "
                f"https://zernio.com (Accounts) or set zernio_account_ids.{platform} in config.json"
            )
            continue
        account = chosen[0]
        account_id = str(account.get("_id") or account.get("id") or "").strip()
        if not account_id:
            errors[platform] = f"failed: Zernio returned a {platform} account without an id"
            continue
        if not account.get("isActive", True):
            errors[platform] = (
                f"failed: the connected {platform} account @{account.get('username', '?')} is inactive — "
                f"reconnect it at https://zernio.com"
            )
            continue
        if len(active) > 1:
            logger.info(
                "Several %s accounts connected to Zernio; using @%s (%s). Set zernio_account_ids to choose.",
                platform, account.get("username", "?"), account_id,
            )
        resolved[platform] = account_id
    return resolved, errors


def presign_upload(api_key: str, base_url: str, filename: str, content_type: str, size: int) -> Dict[str, Any]:
    """``POST /v1/media/presign`` — returns ``uploadUrl`` + ``publicUrl``."""
    body = {"filename": filename, "contentType": content_type, "size": int(size)}
    _, data = _api_request(
        "POST", api_url(base_url, "/media/presign"), api_key, "requesting upload URL", json=body
    )
    if not data.get("uploadUrl") or not data.get("publicUrl"):
        raise ZernioError("requesting upload URL: response lacks uploadUrl/publicUrl", payload=data)
    return data


def put_file(upload_url: str, file_path: str, content_type: str) -> None:
    """``PUT`` the file to the presigned storage URL (no Authorization header)."""
    try:
        with open(file_path, "rb") as fh:
            response = requests.put(
                upload_url,
                data=fh,
                headers={"Content-Type": content_type},
                timeout=UPLOAD_TIMEOUT,
            )
    except requests.exceptions.Timeout as exc:
        raise ZernioError(f"uploading file to storage timed out after {UPLOAD_TIMEOUT}s") from exc
    except requests.exceptions.RequestException as exc:
        raise ZernioError(f"uploading file to storage failed ({str(exc)[:120]})") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise ZernioError(
            f"uploading file to storage failed: HTTP {response.status_code} {response.text[:200]}",
            status=response.status_code,
        )


def upload_media(api_key: str, base_url: str, file_path: str, content_type: str) -> str:
    """Presign + PUT. Returns the public URL to reference in a post."""
    size = os.path.getsize(file_path)
    presigned = presign_upload(api_key, base_url, os.path.basename(file_path), content_type, size)
    put_file(presigned["uploadUrl"], file_path, content_type)
    logger.info("Uploaded %s (%d bytes) to Zernio storage: %s", os.path.basename(file_path), size, presigned["publicUrl"])
    return presigned["publicUrl"]


def create_post(
    api_key: str,
    base_url: str,
    body: Dict[str, Any],
    request_id: Optional[str] = None,
) -> Tuple[int, Dict[str, Any]]:
    """``POST /v1/posts``. Returns ``(status_code, payload)``; 207 = partial."""
    headers = {"x-request-id": request_id or str(uuid.uuid4())}
    return _api_request(
        "POST", api_url(base_url, "/posts"), api_key, "creating post",
        json=body, headers=headers, timeout=PUBLISH_TIMEOUT,
    )


def get_post(api_key: str, base_url: str, post_id: str) -> Dict[str, Any]:
    """``GET /v1/posts/{postId}`` — the current aggregate + per-platform status."""
    _, data = _api_request(
        "GET", api_url(base_url, f"/posts/{post_id}"), api_key, f"checking Zernio post {post_id}"
    )
    post = data.get("post")
    return post if isinstance(post, dict) else data


# ------------------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------------------

def _video_content_type(video_path: str) -> Optional[str]:
    return VIDEO_CONTENT_TYPES.get(os.path.splitext(video_path)[1].lower())


def _image_content_type(image_path: str) -> Optional[str]:
    return IMAGE_CONTENT_TYPES.get(os.path.splitext(image_path)[1].lower())


def _tiktok_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    settings = dict(DEFAULT_TIKTOK_SETTINGS)
    custom = config.get("tiktok_settings")
    if isinstance(custom, dict):
        settings.update(custom)
    return settings


def _config_flag(config: Dict[str, Any], key: str, default: bool) -> bool:
    """Read a boolean config value, tolerating "true"/"false"/1/0 strings."""
    value = config.get(key, None)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def thumbnail_enabled(config: Dict[str, Any]) -> bool:
    """True when the configured default thumbnail should be used as the cover.

    ``use_thumbnail`` defaults to true, so a post carries the user's thumbnail
    (Instagram Reel cover, TikTok cover and the Facebook/YouTube thumbnail)
    unless it is switched off explicitly.
    """
    return _config_flag(config, "use_thumbnail", True)


def _config_number(config: Dict[str, Any], key: str, default: float, minimum: float = 0.0) -> float:
    value = config.get(key, None)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def _platform_names(platforms: List[str]) -> str:
    """Human-readable list: ``["instagram", "tiktok"]`` -> ``"Instagram & TikTok"``."""
    names = [PLATFORM_NAMES.get(p, p.title()) for p in platforms]
    if len(names) <= 1:
        return "".join(names)
    if len(names) == 2:
        return f"{names[0]} & {names[1]}"
    return ", ".join(names[:-1]) + f" & {names[-1]}"


def resolve_thumbnail_path(thumbnail_path: Optional[str]) -> Optional[str]:
    """Absolute path of a configured thumbnail, or ``None`` when it is missing.

    Relative paths are tried against the project directory (the folder holding
    this module) and the current working directory, so the bot and the dashboard
    both find ``thumbnails/default.jpg`` no matter where they are started from.
    """
    if not thumbnail_path or not isinstance(thumbnail_path, str):
        return None
    candidates = [thumbnail_path]
    if not os.path.isabs(thumbnail_path):
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), thumbnail_path))
        candidates.append(os.path.join(os.getcwd(), thumbnail_path))
    for candidate in candidates:
        try:
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)
        except OSError:  # pragma: no cover - defensive (bad path, permissions)
            continue
    return None


# JPEG start-of-frame markers (C0-CF minus the non-frame C4/C8/CC).
_JPEG_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def image_dimensions(path: str) -> Optional[Tuple[int, int]]:
    """``(width, height)`` of a JPEG or PNG file, or ``None`` when unreadable.

    Used to warn when a cover image is not 9:16 — Instagram crops Reel covers
    to 9:16 (1080 x 1920 px), so a wide thumbnail loses its sides.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(32)
            if head.startswith(b"\x89PNG\r\n\x1a\n") and len(head) >= 24:
                width, height = struct.unpack(">II", head[16:24])
                return int(width), int(height)
            if head[:2] == b"\xff\xd8":
                fh.seek(2)
                while True:
                    marker = fh.read(2)
                    if len(marker) < 2 or marker[0] != 0xFF:
                        return None
                    code = marker[1]
                    if code in _JPEG_SOF_MARKERS:
                        fh.read(3)  # segment length (2) + sample precision (1)
                        dimensions = fh.read(4)
                        if len(dimensions) < 4:
                            return None
                        height, width = struct.unpack(">HH", dimensions)
                        return int(width), int(height)
                    length_bytes = fh.read(2)
                    if len(length_bytes) < 2:
                        return None
                    segment_length = struct.unpack(">H", length_bytes)[0]
                    if segment_length < 2:
                        return None
                    fh.seek(segment_length - 2, os.SEEK_CUR)
    except (OSError, struct.error, ValueError):
        return None


def cover_platforms_for(targets: List[str], thumbnail_path: str) -> Tuple[List[str], Dict[str, str]]:
    """Which of ``targets`` can use ``thumbnail_path`` as a cover.

    Returns ``(platforms, skipped_reasons)``. Each platform has its own cover
    field and its own accepted formats (Instagram: JPEG/PNG, TikTok:
    JPG/PNG/WebP up to 20 MB, Facebook/YouTube/LinkedIn: JPG/PNG up to 10 MB).
    """
    extension = os.path.splitext(thumbnail_path)[1].lower()
    try:
        size = os.path.getsize(thumbnail_path)
    except OSError:
        size = 0

    usable: List[str] = []
    skipped: Dict[str, str] = {}
    for platform in targets:
        rule = COVER_RULES.get(platform)
        if not rule:
            continue
        if extension not in rule["extensions"]:
            allowed = "/".join(sorted(e.lstrip(".") for e in rule["extensions"]))
            skipped[platform] = f"{extension or 'no extension'} is not accepted for the {PLATFORM_NAMES.get(platform, platform)} cover (use {allowed})"
            continue
        max_bytes = rule["max_bytes"]
        if max_bytes and size > max_bytes:
            skipped[platform] = f"image is larger than the {max_bytes // (1024 * 1024)} MB {PLATFORM_NAMES.get(platform, platform)} cover limit"
            continue
        usable.append(platform)
    return usable, skipped


def _cover_note(thumbnail_path: str, targets: List[str]) -> Optional[str]:
    """Warn when the cover is far from 9:16, which platforms crop anyway."""
    if "instagram" not in targets:
        return None
    dimensions = image_dimensions(thumbnail_path)
    if not dimensions:
        return None
    width, height = dimensions
    if not height:
        return None
    ratio = width / height
    if 0.52 <= ratio <= 0.61:  # ~9:16
        return None
    return (
        f"thumbnail is {width}x{height}; Instagram shows Reel covers as 9:16 "
        f"(1080x1920) and will centre-crop it"
    )


def platform_outcome(entry: Dict[str, Any]) -> Tuple[str, str]:
    """Translate one ``post.platforms[]`` entry into ``(state, human text)``.

    ``state`` is ``posted``, ``pending`` or ``failed``. "pending" matters: a
    platform that is still ``processing`` has not failed, and treating it as one
    is what made the bot say "Partially posted: 1/2 platforms succeeded" while
    Instagram was simply still publishing.
    """
    status = str(entry.get("status") or "").lower()
    url = entry.get("platformPostUrl") or entry.get("url")
    if status in PUBLISHED_PLATFORM_STATUSES:
        return "posted", f"posted — {url}" if url else "posted"
    if status in {"failed", "cancelled", "error"}:
        reason = entry.get("errorMessage") or entry.get("error") or entry.get("message") or "platform rejected the post"
        category = entry.get("errorCategory")
        return "failed", f"failed: {reason}" + (f" [{category}]" if category else "")
    if status in PENDING_PLATFORM_STATUSES:
        return "pending", f"{status} — check the Zernio dashboard"
    return "failed", f"unknown status: {status or 'n/a'}"


def _entry_account_id(entry: Dict[str, Any]) -> str:
    """``accountId`` is a plain string on create and an object on GET /posts."""
    account = entry.get("accountId") or entry.get("account_id")
    if isinstance(account, dict):
        account = account.get("_id") or account.get("id") or ""
    return str(account or "")


def _platform_results(payload: Dict[str, Any], targets: Dict[str, str]) -> Dict[str, Tuple[str, str]]:
    """Match the API's per-platform entries back to the platforms we requested."""
    post = payload.get("post") if isinstance(payload.get("post"), dict) else payload
    entries = post.get("platforms") if isinstance(post.get("platforms"), list) else []
    # Some responses also carry a flat results array; use it as a fallback.
    if not entries and isinstance(payload.get("results"), list):
        entries = payload["results"]

    results: Dict[str, Tuple[str, str]] = {}
    for platform, account_id in targets.items():
        same_platform = [
            e for e in entries
            if isinstance(e, dict) and str(e.get("platform", "")).lower() == platform
        ]
        exact = [e for e in same_platform if _entry_account_id(e) == account_id]
        match = (exact or same_platform or [None])[0]
        if match is None:
            overall = str(post.get("status") or "").lower()
            if overall in PUBLISHED_PLATFORM_STATUSES:
                results[platform] = ("posted", "posted")
            elif overall in PENDING_PLATFORM_STATUSES:
                results[platform] = ("pending", f"{overall} — check the Zernio dashboard")
            else:
                results[platform] = ("failed", f"{overall or 'unknown'} (no per-platform result returned)")
        else:
            results[platform] = platform_outcome(match)
    return results


def _status_wait_config(config: Dict[str, Any]) -> Tuple[float, float]:
    """``(how long to poll, seconds between polls)`` for a still-processing post."""
    wait = _config_number(config, "post_status_wait_seconds", float(DEFAULT_STATUS_WAIT_SECONDS))
    interval = _config_number(config, "post_status_poll_interval", DEFAULT_STATUS_POLL_INTERVAL, minimum=0.5)
    return min(wait, float(MAX_STATUS_WAIT_SECONDS)), interval


def _wait_for_post(
    api_key: str,
    base_url: str,
    post_id: str,
    targets: Dict[str, str],
    wait_seconds: float,
    interval: float,
) -> Optional[Dict[str, Any]]:
    """Poll ``GET /v1/posts/{postId}`` until every target is terminal.

    Returns the last post payload, or ``None`` when the API could not be
    reached (the caller then keeps the results from the create response).
    """
    deadline = time.monotonic() + wait_seconds
    last_post: Optional[Dict[str, Any]] = None
    while True:
        try:
            last_post = get_post(api_key, base_url, post_id)
        except ZernioError as exc:
            logger.warning("Could not refresh Zernio post %s: %s", post_id, exc)
            return last_post
        states = _platform_results({"post": last_post}, targets)
        pending = [p for p, (state, _) in states.items() if state == "pending"]
        if not pending:
            return last_post
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.info(
                "Zernio post %s is still processing %s after %.0fs; reporting the current status",
                post_id, _platform_names(pending), wait_seconds,
            )
            return last_post
        time.sleep(min(interval, remaining))


def _summary(posted: List[str], pending: List[str], failed: List[str], total: int) -> str:
    if total and len(posted) == total:
        return f"Posted to {total}/{total} platforms"
    if posted and not pending:
        return f"Partially posted: {len(posted)}/{total} platforms succeeded"
    if posted and pending:
        text = f"Posted to {len(posted)}/{total} platforms — {_platform_names(pending)} still processing"
        if failed:
            text += f", {_platform_names(failed)} failed"
        return text
    if pending:
        text = f"Still processing: {_platform_names(pending)}"
        if failed:
            text += f" ({_platform_names(failed)} failed)"
        return text
    return f"Failed to post to all {total} platforms"


def _finalize(
    results: Dict[str, str],
    states: Dict[str, str],
    post_to: List[str],
    message: str,
    **extra,
) -> Dict[str, Any]:
    """Assemble the dict the Telegram bot and scripts consume.

    ``success`` is True as soon as one platform published; ``posted`` /
    ``pending`` / ``failed`` list the platforms per state so callers can phrase
    "still processing" differently from "failed".
    """
    posted = [p for p in post_to if states.get(p) == "posted"]
    pending = [p for p in post_to if states.get(p) == "pending"]
    failed = [p for p in post_to if p not in posted and p not in pending]

    out: Dict[str, Any] = {
        "success": bool(posted),
        "message": message,
        "posted": posted,
        "pending": pending,
        "failed": failed,
    }
    out.update(results)
    # Backward compatibility for the Telegram reply: always expose both keys.
    for platform in ("instagram", "tiktok"):
        out.setdefault(platform, "skipped" if platform not in post_to else "failed: not attempted")
    out["platforms"] = list(post_to)
    out.update(extra)
    return out


# ------------------------------------------------------------------------------
# Public entry point
# ------------------------------------------------------------------------------

def upload_video(video_path: str, caption: str, thumbnail_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Publish ``video_path`` with ``caption`` to every platform in ``post_to``.

    Returns a dict compatible with the previous implementation::

        {
            "success": True/False,            # at least one platform published
            "message": "Posted to 2/2 platforms",
            "instagram": "posted — https://www.instagram.com/p/...",
            "tiktok": "posted" | "failed: <reason>" | "skipped",
            "platforms": ["instagram", "tiktok"],
            "posted": ["instagram", "tiktok"],   # platforms that published
            "pending": [],                       # still processing at return time
            "failed": [],
            "thumbnail": {                       # what happened to the cover
                "name": "default.jpg",
                "platforms": ["instagram", "tiktok"],
                "url": "https://media.zernio.com/temp/...",
                "note": None,
            },
            "post_id": "65f1c0a9e2b5af0012ab34cd",   # when a post was created
            "post_status": "published" | "publishing" | "partial" | "failed",
        }
    """
    # -- configuration ----------------------------------------------------------
    try:
        config = load_config()
    except Exception as e:
        return _finalize({}, {}, ["instagram", "tiktok"], f"Failed to load config: {e}")

    api_key = str(config.get("zernio_api_key") or "").strip()
    if api_key in PLACEHOLDER_API_KEYS:
        msg = "Zernio API key not configured. Set it in config.json or via the dashboard."
        return _finalize({}, {}, ["instagram", "tiktok"], msg)

    post_to_raw = config.get("post_to", ["instagram", "tiktok"])
    if not isinstance(post_to_raw, list) or not post_to_raw:
        post_to_raw = ["instagram", "tiktok"]
    post_to: List[str] = []
    for p in post_to_raw:
        p = str(p).lower().strip()
        if p and p not in post_to:
            post_to.append(p)

    try:
        base_url = normalize_api_base_url(config.get("zernio_api_base_url"))
    except ValueError as e:
        return _finalize({}, {}, post_to, f"Invalid Zernio API base URL: {e}")

    results: Dict[str, str] = {}
    states: Dict[str, str] = {}
    targets_wanted: List[str] = []
    for platform in post_to:
        if platform in SUPPORTED_PLATFORMS:
            targets_wanted.append(platform)
        else:
            results[platform] = "failed: unsupported platform"
            states[platform] = "failed"
            logger.warning("Unsupported platform %s in post_to, skipping", platform)

    if not targets_wanted:
        return _finalize(results, states, post_to, "No supported platforms configured in post_to")

    # -- video file -------------------------------------------------------------
    if not video_path or not os.path.exists(video_path):
        for platform in targets_wanted:
            results[platform] = "failed: video file missing"
            states[platform] = "failed"
        return _finalize(results, states, post_to, f"Video file not found: {video_path}")

    content_type = _video_content_type(video_path)
    if not content_type:
        ext = os.path.splitext(video_path)[1] or "(none)"
        for platform in targets_wanted:
            results[platform] = f"failed: unsupported video format {ext}"
            states[platform] = "failed"
        return _finalize(
            results, states, post_to,
            f"Unsupported video format {ext}; Zernio accepts MP4, MOV, WebM, M4V, AVI, MPEG",
        )

    # -- 1. account ids ---------------------------------------------------------
    account_ids, account_errors = resolve_account_ids(targets_wanted, config, api_key, base_url)
    for platform, error in account_errors.items():
        results[platform] = error
        states[platform] = "failed"
    targets = {p: account_ids[p] for p in targets_wanted if p in account_ids}
    if not targets:
        first_error = next(iter(account_errors.values()), "no Zernio accounts resolved")
        return _finalize(results, states, post_to, first_error.replace("failed: ", "", 1))

    # -- 2./3. upload media -----------------------------------------------------
    try:
        media_url = upload_media(api_key, base_url, video_path, content_type)
    except ZernioError as e:
        logger.error("Zernio media upload failed: %s", e)
        for platform in targets:
            results[platform] = f"failed: {e}"
            states[platform] = "failed"
        return _finalize(results, states, post_to, f"Upload to Zernio storage failed: {e}")
    except OSError as e:
        for platform in targets:
            results[platform] = f"failed: could not read video file ({e})"
            states[platform] = "failed"
        return _finalize(results, states, post_to, f"Could not read video file: {e}")

    # -- cover image (Instagram Reel cover / TikTok cover / FB-YT thumbnail) ----
    thumbnail_info: Optional[Dict[str, Any]] = None
    cover_platforms: List[str] = []
    thumbnail_url: Optional[str] = None
    use_thumbnail = thumbnail_enabled(config)
    if config.get("tiktok_cover_from_thumbnail") is not None:
        logger.info(
            "config key tiktok_cover_from_thumbnail is deprecated and ignored; "
            "use_thumbnail=%s controls every cover now", use_thumbnail,
        )
    if thumbnail_path is None:
        thumbnail_path = config.get("default_thumbnail", "thumbnails/default.jpg")

    if not use_thumbnail:
        logger.info("use_thumbnail is false in config.json — posting without a custom cover")
        thumbnail_info = {"name": None, "platforms": [], "url": None, "note": "covers disabled (use_thumbnail: false)"}
    else:
        resolved_thumbnail = resolve_thumbnail_path(thumbnail_path)
        if not resolved_thumbnail:
            logger.warning("Thumbnail not found at %s, posting without a cover", thumbnail_path)
            thumbnail_info = {"name": thumbnail_path, "platforms": [], "url": None,
                              "note": f"image not found ({thumbnail_path})"}
        else:
            cover_platforms, skipped = cover_platforms_for(list(targets), resolved_thumbnail)
            for platform, reason in skipped.items():
                logger.warning("Skipping the %s cover: %s", platform, reason)
            if not cover_platforms:
                thumbnail_info = {"name": os.path.basename(resolved_thumbnail), "platforms": [], "url": None,
                                  "note": "; ".join(skipped.values()) or "no target platform takes a cover"}
            else:
                thumb_type = _image_content_type(resolved_thumbnail)
                try:
                    thumbnail_url = upload_media(api_key, base_url, resolved_thumbnail, thumb_type)
                    thumbnail_info = {
                        "name": os.path.basename(resolved_thumbnail),
                        "platforms": cover_platforms,
                        "url": thumbnail_url,
                        "note": _cover_note(resolved_thumbnail, cover_platforms),
                    }
                except (ZernioError, OSError) as e:
                    logger.warning("Cover upload failed, continuing without it: %s", e)
                    cover_platforms = []
                    thumbnail_info = {"name": os.path.basename(resolved_thumbnail), "platforms": [], "url": None,
                                      "note": f"upload failed ({e})"}

    # -- 4. create + publish the post ------------------------------------------
    media_item: Dict[str, Any] = {"url": media_url, "type": "video"}
    if thumbnail_url and any(p in THUMBNAIL_PLATFORMS for p in cover_platforms):
        media_item["thumbnail"] = thumbnail_url

    platforms_body: List[Dict[str, Any]] = []
    for platform, account_id in targets.items():
        entry: Dict[str, Any] = {"platform": platform, "accountId": account_id}
        platform_data: Dict[str, Any] = {}
        if platform == "instagram":
            ig_settings = config.get("instagram_settings")
            if isinstance(ig_settings, dict) and ig_settings:
                platform_data.update(ig_settings)
            # Instagram ignores mediaItems[].thumbnail; the Reel cover goes in
            # platformSpecificData.instagramThumbnail (alias: reelCover). An
            # explicit value from instagram_settings wins over the default.
            if thumbnail_url and "instagram" in cover_platforms:
                platform_data.setdefault("instagramThumbnail", thumbnail_url)
        if platform_data:
            entry["platformSpecificData"] = platform_data
        platforms_body.append(entry)

    body: Dict[str, Any] = {
        "content": caption or "",
        "mediaItems": [media_item],
        "platforms": platforms_body,
        "publishNow": True,
    }
    if "tiktok" in targets:
        tiktok_settings = _tiktok_settings(config)
        if thumbnail_url and "tiktok" in cover_platforms:
            # TikTok takes the cover image itself; it never reads mediaItems.
            tiktok_settings.setdefault("video_cover_image_url", thumbnail_url)
        body["tiktokSettings"] = tiktok_settings

    request_id = str(uuid.uuid4())
    try:
        status_code, payload = create_post(api_key, base_url, body, request_id)
    except ZernioError as e:
        logger.error("Zernio create post failed: %s", e)
        if e.status == 409:
            existing = e.payload.get("existingPostId") or "?"
            culprit = str(e.payload.get("platform") or "").lower()
            text = (
                f"failed: duplicate — the same video + caption was already posted to this account "
                f"in the last 24h (Zernio post {existing}). Change the caption to repost."
            )
            for platform in targets:
                results[platform] = text if (not culprit or culprit == platform) else "failed: not attempted (duplicate on another platform)"
                states[platform] = "failed"
            return _finalize(results, states, post_to, "Duplicate post rejected by Zernio (24h window)",
                             post_id=existing, thumbnail=thumbnail_info)
        for platform in targets:
            results[platform] = f"failed: {e}"
            states[platform] = "failed"
        return _finalize(results, states, post_to, f"Zernio rejected the post: {e}", thumbnail=thumbnail_info)

    # A retried x-request-id returns 200 with the original post.
    if status_code == 200 and isinstance(payload.get("existingPost"), dict):
        payload = {"post": payload["existingPost"]}

    post = payload.get("post") if isinstance(payload.get("post"), dict) else {}
    post_id = str(post.get("_id") or post.get("id") or "")
    post_status = str(post.get("status") or ("published" if status_code == 201 else "partial" if status_code == 207 else "unknown"))

    per_platform = _platform_results(payload, targets)

    # -- 5. still processing? poll GET /v1/posts/{postId} ----------------------
    if post_id and any(state == "pending" for state, _ in per_platform.values()):
        wait_seconds, interval = _status_wait_config(config)
        still = [p for p, (state, _) in per_platform.items() if state == "pending"]
        if wait_seconds > 0:
            logger.info(
                "Zernio post %s: %s still publishing — polling for up to %.0fs",
                post_id, _platform_names(still), wait_seconds,
            )
            final_post = _wait_for_post(api_key, base_url, post_id, targets, wait_seconds, interval)
            if final_post:
                post_status = str(final_post.get("status") or post_status)
                per_platform = _platform_results({"post": final_post}, targets)
        else:
            logger.info("Zernio post %s: %s still publishing (polling disabled)", post_id, _platform_names(still))

    for platform, (state, text) in per_platform.items():
        states[platform] = state
        results[platform] = text
        if state == "posted":
            logger.info("Zernio: %s -> %s", platform, text)
        elif state == "pending":
            logger.warning("Zernio: %s -> %s", platform, text)
        else:
            logger.error("Zernio: %s -> %s", platform, text)

    posted = [p for p in post_to if states.get(p) == "posted"]
    pending = [p for p in post_to if states.get(p) == "pending"]
    failed = [p for p in post_to if p not in posted and p not in pending]

    # Express the aggregate status from what we now know (the API's value can
    # still say "publishing" while every target has in fact finished).
    if per_platform:
        if posted and not pending and not failed:
            post_status = "published"
        elif not posted and not pending:
            post_status = "failed"
        elif not pending:
            post_status = "partial"

    message = _summary(posted, pending, failed, len(post_to))
    if post_id:
        message += f" (Zernio post {post_id}, status {post_status})"
    return _finalize(
        results, states, post_to, message,
        post_id=post_id, post_status=post_status, thumbnail=thumbnail_info,
    )
