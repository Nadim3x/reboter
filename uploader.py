# TODO: Replace API endpoint and field names with actual
# Zernio API docs from your account dashboard
"""
uploader.py — Zernio API Uploader

Uploads videos to Instagram and TikTok via Zernio API.
"""

import os
import logging
import requests
from typing import Dict, Any

from caption import load_config

# Setup logger
logger = logging.getLogger(__name__)


def upload_video(video_path: str, caption: str, thumbnail_path: str = None) -> Dict[str, Any]:
    """
    Upload video to Instagram and TikTok via Zernio API.

    Args:
        video_path: Path to downloaded video file
        caption: Caption text to use
        thumbnail_path: Path to thumbnail image (optional, uses config default if None)

    Returns:
        dict: {
            "success": True/False,
            "instagram": "posted" / "failed: [error]",
            "tiktok": "posted" / "failed: [error]",
            "message": "Posted to 2/2 platforms"
        }
    """
    # Load config to get API key and platforms
    try:
        config = load_config()
    except Exception as e:
        return {
            "success": False,
            "message": f"Failed to load config: {e}",
            "instagram": f"failed: config error",
            "tiktok": f"failed: config error",
        }

    # Get Zernio API key
    zernio_api_key = config.get("zernio_api_key", "")
    if not zernio_api_key or zernio_api_key == "YOUR_ZERNIO_API_KEY" or zernio_api_key.strip() == "":
        return {
            "success": False,
            "message": "Zernio API key not configured. Set it in config.json or via dashboard.",
            "instagram": "failed: API key missing",
            "tiktok": "failed: API key missing",
        }

    # Get platforms to post to
    post_to = config.get("post_to", ["instagram", "tiktok"])
    if not isinstance(post_to, list) or len(post_to) == 0:
        post_to = ["instagram", "tiktok"]

    # Resolve thumbnail path
    if thumbnail_path is None:
        thumbnail_path = config.get("default_thumbnail", "thumbnails/default.jpg")

    # Validate video file exists
    if not os.path.exists(video_path):
        return {
            "success": False,
            "message": f"Video file not found: {video_path}",
            "instagram": "failed: video file missing",
            "tiktok": "failed: video file missing",
        }

    # Check if thumbnail exists, if not, proceed without it (some APIs allow)
    thumbnail_exists = os.path.exists(thumbnail_path) if thumbnail_path else False
    if not thumbnail_exists:
        logger.warning(f"Thumbnail not found at {thumbnail_path}, uploading without thumbnail")

    # Zernio API endpoint - TODO: Update with real endpoint
    # Placeholder URL - replace with actual from Zernio dashboard
    api_url = "https://api.zernio.com/v1/upload"

    # Results tracking
    results = {}
    success_count = 0

    # Upload to each platform
    for platform in post_to:
        platform = platform.lower().strip()
        if platform not in ["instagram", "tiktok", "youtube", "facebook"]:
            logger.warning(f"Unsupported platform {platform}, skipping")
            results[platform] = f"failed: unsupported platform"
            continue

        try:
            # Prepare headers
            headers = {
                "Authorization": f"Bearer {zernio_api_key}"
            }

            # Prepare multipart form data
            # Note: field names may need to be updated based on real Zernio API docs
            files = {}
            data = {
                "caption": caption,
                "platform": platform,
            }

            # Open video file
            try:
                video_file = open(video_path, "rb")
                files["video"] = (os.path.basename(video_path), video_file, "video/mp4")
            except Exception as e:
                results[platform] = f"failed: could not open video file - {e}"
                logger.error(f"Failed to open video file {video_path}: {e}")
                continue

            # Open thumbnail file if exists
            thumb_file = None
            if thumbnail_exists:
                try:
                    thumb_file = open(thumbnail_path, "rb")
                    # Determine mime type from extension
                    ext = os.path.splitext(thumbnail_path)[1].lower()
                    mime_map = {
                        ".jpg": "image/jpeg",
                        ".jpeg": "image/jpeg",
                        ".png": "image/png",
                        ".webp": "image/webp",
                    }
                    mime_type = mime_map.get(ext, "image/jpeg")
                    files["thumbnail"] = (os.path.basename(thumbnail_path), thumb_file, mime_type)
                except Exception as e:
                    logger.warning(f"Failed to open thumbnail {thumbnail_path}: {e}")
                    # Continue without thumbnail
                    if thumb_file:
                        try:
                            thumb_file.close()
                        except:
                            pass

            try:
                # Make POST request to Zernio API
                # TODO: Verify endpoint, headers, and field names with Zernio docs
                response = requests.post(
                    api_url,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=60,  # 60 second timeout for upload
                )

                # Check response status
                if response.status_code in [200, 201]:
                    results[platform] = "posted"
                    success_count += 1
                    logger.info(f"Successfully posted to {platform}")
                else:
                    # Try to get error message from response
                    try:
                        error_detail = response.json().get("message", response.text[:200])
                    except:
                        error_detail = response.text[:200]
                    error_msg = f"API returned {response.status_code}: {error_detail}"
                    results[platform] = f"failed: {error_msg}"
                    logger.error(f"Failed to post to {platform}: {error_msg}")

            except requests.exceptions.Timeout:
                error_msg = "Request timed out"
                results[platform] = f"failed: {error_msg}"
                logger.error(f"Timeout uploading to {platform}")
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Connection error: {str(e)[:100]}"
                results[platform] = f"failed: {error_msg}"
                logger.error(f"Connection error uploading to {platform}: {e}")
            except Exception as e:
                error_msg = f"Upload error: {str(e)[:200]}"
                results[platform] = f"failed: {error_msg}"
                logger.error(f"Error uploading to {platform}: {e}")
            finally:
                # Always close files
                try:
                    video_file.close()
                except:
                    pass
                if thumb_file:
                    try:
                        thumb_file.close()
                    except:
                        pass

        except Exception as e:
            # Outer exception for platform loop
            error_msg = f"Unexpected error: {str(e)[:200]}"
            results[platform] = f"failed: {error_msg}"
            logger.error(f"Unexpected error for platform {platform}: {e}")

    # Determine overall success
    overall_success = success_count > 0
    total_platforms = len(post_to)

    # Build message
    if success_count == total_platforms:
        message = f"Posted to {success_count}/{total_platforms} platforms"
    elif success_count > 0:
        message = f"Partially posted: {success_count}/{total_platforms} platforms succeeded"
    else:
        message = f"Failed to post to all {total_platforms} platforms"

    # Build return dict
    return_dict = {
        "success": overall_success,
        "message": message,
    }
    # Add per-platform results
    return_dict.update(results)

    # Ensure instagram and tiktok keys exist for backward compatibility
    if "instagram" not in return_dict:
        return_dict["instagram"] = "skipped" if "instagram" not in post_to else "failed: not attempted"
    if "tiktok" not in return_dict:
        return_dict["tiktok"] = "skipped" if "tiktok" not in post_to else "failed: not attempted"

    return return_dict
