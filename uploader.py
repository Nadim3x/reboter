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

There is no ``/v1/upload`` endpoint on Zernio: the previous implementation
posted a multipart form there and every upload failed with
``404 endpoint_not_found``.

Base URL: ``https://zernio.com/api/v1``. Legacy values that older versions of
this project wrote to config.json (``https://api.zernio.com``, ``.../upload``)
are normalised automatically, see :func:`normalize_api_base_url`.

Configuration keys read from config.json:

    zernio_api_key        "sk_..." from https://zernio.com/dashboard/api-keys
    zernio_api_base_url   defaults to https://zernio.com/api/v1
    post_to               ["instagram", "tiktok"]
    zernio_account_ids    optional {"instagram": "<24-char id>", ...} overrides;
                          otherwise the first active account per platform is used
    tiktok_settings       merged over DEFAULT_TIKTOK_SETTINGS (TikTok requires
                          privacy_level + the two consent flags on every post)
    instagram_settings    optional platformSpecificData for Instagram
                          (e.g. {"shareToFeed": false})
    tiktok_cover_from_thumbnail
                          true -> the default thumbnail is uploaded and used as
                          the TikTok video cover (video_cover_image_url)
"""

import os
import json
import uuid
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

# ``mediaItems[].thumbnail`` is only honoured by these platforms (docs: Media
# Uploads guide). Instagram and TikTok ignore it; TikTok has its own
# ``video_cover_image_url`` setting instead.
THUMBNAIL_PLATFORMS = {"facebook", "youtube", "linkedin"}

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


def _platform_outcome(entry: Dict[str, Any]) -> Tuple[bool, str]:
    """Translate one ``post.platforms[]`` entry into (success, human text)."""
    status = str(entry.get("status") or "").lower()
    url = entry.get("platformPostUrl") or entry.get("url")
    if status == "published":
        return True, f"posted — {url}" if url else "posted"
    if status == "failed":
        reason = entry.get("errorMessage") or entry.get("error") or entry.get("message") or "platform rejected the post"
        category = entry.get("errorCategory")
        return False, f"failed: {reason}" + (f" [{category}]" if category else "")
    if status in {"pending", "publishing", "scheduled", "processing"}:
        return False, f"{status} — check the Zernio dashboard"
    return False, f"unknown status: {status or 'n/a'}"


def _platform_results(payload: Dict[str, Any], targets: Dict[str, str]) -> Dict[str, Tuple[bool, str]]:
    """Match the API's per-platform entries back to the platforms we requested."""
    post = payload.get("post") if isinstance(payload.get("post"), dict) else payload
    entries = post.get("platforms") if isinstance(post.get("platforms"), list) else []
    # Some responses also carry a flat results array; use it as a fallback.
    if not entries and isinstance(payload.get("results"), list):
        entries = payload["results"]

    results: Dict[str, Tuple[bool, str]] = {}
    for platform, account_id in targets.items():
        same_platform = [
            e for e in entries
            if isinstance(e, dict) and str(e.get("platform", "")).lower() == platform
        ]
        exact = [
            e for e in same_platform
            if str(e.get("accountId") or e.get("account_id") or "") == account_id
        ]
        match = (exact or same_platform or [None])[0]
        if match is None:
            overall = str(post.get("status") or "").lower()
            results[platform] = (overall == "published", f"{overall or 'unknown'} (no per-platform result returned)")
        else:
            results[platform] = _platform_outcome(match)
    return results


def _summary(success_count: int, total: int) -> str:
    if total and success_count == total:
        return f"Posted to {success_count}/{total} platforms"
    if success_count > 0:
        return f"Partially posted: {success_count}/{total} platforms succeeded"
    return f"Failed to post to all {total} platforms"


def _finalize(results: Dict[str, str], post_to: List[str], success_count: int, message: str, **extra) -> Dict[str, Any]:
    out: Dict[str, Any] = {"success": success_count > 0, "message": message}
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
            "post_id": "65f1c0a9e2b5af0012ab34cd",   # when a post was created
            "post_status": "published" | "partial" | "failed",
        }
    """
    # -- configuration ----------------------------------------------------------
    try:
        config = load_config()
    except Exception as e:
        return _finalize({}, ["instagram", "tiktok"], 0, f"Failed to load config: {e}")

    api_key = str(config.get("zernio_api_key") or "").strip()
    if api_key in PLACEHOLDER_API_KEYS:
        msg = "Zernio API key not configured. Set it in config.json or via the dashboard."
        return _finalize({}, ["instagram", "tiktok"], 0, msg)

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
        return _finalize({}, post_to, 0, f"Invalid Zernio API base URL: {e}")

    results: Dict[str, str] = {}
    targets_wanted: List[str] = []
    for platform in post_to:
        if platform in SUPPORTED_PLATFORMS:
            targets_wanted.append(platform)
        else:
            results[platform] = "failed: unsupported platform"
            logger.warning("Unsupported platform %s in post_to, skipping", platform)

    if not targets_wanted:
        return _finalize(results, post_to, 0, "No supported platforms configured in post_to")

    # -- video file -------------------------------------------------------------
    if not video_path or not os.path.exists(video_path):
        for platform in targets_wanted:
            results[platform] = "failed: video file missing"
        return _finalize(results, post_to, 0, f"Video file not found: {video_path}")

    content_type = _video_content_type(video_path)
    if not content_type:
        ext = os.path.splitext(video_path)[1] or "(none)"
        for platform in targets_wanted:
            results[platform] = f"failed: unsupported video format {ext}"
        return _finalize(results, post_to, 0, f"Unsupported video format {ext}; Zernio accepts MP4, MOV, WebM, M4V, AVI, MPEG")

    # -- 1. account ids ---------------------------------------------------------
    account_ids, account_errors = resolve_account_ids(targets_wanted, config, api_key, base_url)
    results.update(account_errors)
    targets = {p: account_ids[p] for p in targets_wanted if p in account_ids}
    if not targets:
        first_error = next(iter(account_errors.values()), "no Zernio accounts resolved")
        return _finalize(results, post_to, 0, first_error.replace("failed: ", "", 1))

    # -- 2./3. upload media -----------------------------------------------------
    try:
        media_url = upload_media(api_key, base_url, video_path, content_type)
    except ZernioError as e:
        logger.error("Zernio media upload failed: %s", e)
        for platform in targets:
            results[platform] = f"failed: {e}"
        return _finalize(results, post_to, 0, f"Upload to Zernio storage failed: {e}")
    except OSError as e:
        for platform in targets:
            results[platform] = f"failed: could not read video file ({e})"
        return _finalize(results, post_to, 0, f"Could not read video file: {e}")

    # Optional thumbnail: only uploaded when a target platform can use it.
    if thumbnail_path is None:
        thumbnail_path = config.get("default_thumbnail", "thumbnails/default.jpg")
    if thumbnail_path and not os.path.isabs(thumbnail_path):
        candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), thumbnail_path)
        if os.path.exists(candidate):
            thumbnail_path = candidate
    thumbnail_url: Optional[str] = None
    want_tiktok_cover = bool(config.get("tiktok_cover_from_thumbnail", False)) and "tiktok" in targets
    want_thumbnail = want_tiktok_cover or any(p in THUMBNAIL_PLATFORMS for p in targets)
    if want_thumbnail and thumbnail_path and os.path.exists(thumbnail_path):
        thumb_type = _image_content_type(thumbnail_path)
        if thumb_type:
            try:
                thumbnail_url = upload_media(api_key, base_url, thumbnail_path, thumb_type)
            except (ZernioError, OSError) as e:
                logger.warning("Thumbnail upload failed, continuing without it: %s", e)
        else:
            logger.warning("Thumbnail %s has an unsupported extension, skipping", thumbnail_path)
    elif want_thumbnail and thumbnail_path:
        logger.warning("Thumbnail not found at %s, posting without it", thumbnail_path)

    # -- 4. create + publish the post ------------------------------------------
    media_item: Dict[str, Any] = {"url": media_url, "type": "video"}
    if thumbnail_url and any(p in THUMBNAIL_PLATFORMS for p in targets):
        media_item["thumbnail"] = thumbnail_url

    platforms_body: List[Dict[str, Any]] = []
    for platform, account_id in targets.items():
        entry: Dict[str, Any] = {"platform": platform, "accountId": account_id}
        if platform == "instagram":
            ig_settings = config.get("instagram_settings")
            if isinstance(ig_settings, dict) and ig_settings:
                entry["platformSpecificData"] = dict(ig_settings)
        platforms_body.append(entry)

    body: Dict[str, Any] = {
        "content": caption or "",
        "mediaItems": [media_item],
        "platforms": platforms_body,
        "publishNow": True,
    }
    if "tiktok" in targets:
        tiktok_settings = _tiktok_settings(config)
        if want_tiktok_cover and thumbnail_url:
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
            return _finalize(results, post_to, 0, "Duplicate post rejected by Zernio (24h window)", post_id=existing)
        for platform in targets:
            results[platform] = f"failed: {e}"
        return _finalize(results, post_to, 0, f"Zernio rejected the post: {e}")

    # A retried x-request-id returns 200 with the original post.
    if status_code == 200 and isinstance(payload.get("existingPost"), dict):
        payload = {"post": payload["existingPost"]}

    post = payload.get("post") if isinstance(payload.get("post"), dict) else {}
    post_id = str(post.get("_id") or post.get("id") or "")
    post_status = str(post.get("status") or ("published" if status_code == 201 else "partial" if status_code == 207 else "unknown"))

    per_platform = _platform_results(payload, targets)
    success_count = 0
    for platform, (ok, text) in per_platform.items():
        results[platform] = text
        if ok:
            success_count += 1
            logger.info("Zernio: %s -> %s", platform, text)
        else:
            logger.error("Zernio: %s -> %s", platform, text)

    message = _summary(success_count, len(post_to))
    if post_id:
        message += f" (Zernio post {post_id}, status {post_status})"
    return _finalize(results, post_to, success_count, message, post_id=post_id, post_status=post_status)
