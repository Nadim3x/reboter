"""
downloader.py — yt-dlp Video Downloader

Downloads videos from YouTube, Instagram, TikTok, Facebook Reels
and all other yt-dlp supported sources.
"""

import os
import glob
import yt_dlp
from yt_dlp.utils import DownloadError
from typing import Dict, Any


# Ensure downloads directory exists
DOWNLOADS_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)


def download_video(url: str) -> Dict[str, Any]:
    """
    Download video from URL using yt-dlp.

    Args:
        url: Video URL to download (YouTube, TikTok, Instagram, etc.)

    Returns:
        dict: {
            "video_path": "downloads/xxxxx.mp4",
            "original_caption": "text or None",
            "title": "video title",
            "duration": 45
        }

    Raises:
        ValueError: For invalid URL, too large file, or unavailable video
    """
    # Validate URL is not empty
    if not url or not isinstance(url, str) or url.strip() == "":
        raise ValueError("Invalid or unsupported URL")

    url = url.strip()

    # yt-dlp options as specified
    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOADS_DIR, "%(id)s.%(ext)s"),
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "max_filesize": 100 * 1024 * 1024,  # 100MB
        "quiet": True,
        "no_warnings": True,
        "writeinfojson": False,
        "noplaylist": True,  # Don't download playlists, just single video
        "merge_output_format": "mp4",
    }

    try:
        # Use YoutubeDL to extract info and download
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # First extract info without downloading to get metadata
            try:
                info = ydl.extract_info(url, download=False)
            except DownloadError as e:
                error_msg = str(e).lower()
                # Check for specific error types
                if "private" in error_msg or "unavailable" in error_msg or "deleted" in error_msg or "not available" in error_msg:
                    raise ValueError("Video unavailable")
                elif "unsupported url" in error_msg or "no video" in error_msg:
                    raise ValueError("Invalid or unsupported URL")
                elif "file is too large" in error_msg or "max_filesize" in error_msg or "too large" in error_msg:
                    raise ValueError("Video too large (max 100MB)")
                else:
                    # Generic download error -> treat as unavailable or invalid
                    if "video unavailable" in error_msg or "private" in error_msg:
                        raise ValueError("Video unavailable")
                    raise ValueError(f"Invalid or unsupported URL: {str(e)[:200]}")

            # Handle playlists: if extractor returns playlist, take first entry
            if info is None:
                raise ValueError("Invalid or unsupported URL")

            if "entries" in info:
                # It's a playlist, take first video
                entries = list(info["entries"])
                if not entries:
                    raise ValueError("Video unavailable")
                info = entries[0]

            # Extract caption/description
            original_caption = info.get("description", None)
            # Normalize empty string to None
            if isinstance(original_caption, str) and original_caption.strip() == "":
                original_caption = None

            # Extract other metadata
            title = info.get("title", "Unknown Title")
            duration = info.get("duration", 0)
            video_id = info.get("id", "unknown")

            # Now download the actual video file
            try:
                # Need to re-extract with download=True, or use download method
                # Using ydl.download([url]) after extracting info
                # But we already have info, so we can use ydl.process_info or re-extract
                # Simplest: call extract_info with download=True for same URL
                # However to avoid double extraction, we use ydl.download

                # ydl.download expects list of URLs
                # We'll create a new YDL instance for download to ensure fresh state
                ydl.download([url])

            except DownloadError as e:
                error_msg = str(e).lower()
                if "too large" in error_msg or "max_filesize" in error_msg or "file is too large" in error_msg:
                    raise ValueError("Video too large (max 100MB)")
                elif "private" in error_msg or "unavailable" in error_msg or "deleted" in error_msg:
                    raise ValueError("Video unavailable")
                else:
                    raise ValueError(f"Download failed: {str(e)[:300]}")

            # Find the output file path
            # Expected pattern: downloads/{id}.*
            # Try to locate the file
            possible_patterns = [
                os.path.join(DOWNLOADS_DIR, f"{video_id}.*"),
                os.path.join(DOWNLOADS_DIR, f"*.mp4"),
            ]

            video_path = None

            # First try exact id match
            matches = glob.glob(os.path.join(DOWNLOADS_DIR, f"{video_id}.*"))
            # Filter out json files etc, keep video files
            video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4a"}
            video_matches = [m for m in matches if os.path.splitext(m)[1].lower() in video_exts or os.path.splitext(m)[1].lower() == ".mp4"]

            # If not found by id, search by most recent file
            if not video_matches:
                all_videos = []
                for ext in ["*.mp4", "*.mkv", "*.webm", "*.mov"]:
                    all_videos.extend(glob.glob(os.path.join(DOWNLOADS_DIR, ext)))
                if all_videos:
                    # Sort by modification time, newest first
                    all_videos.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                    video_path = all_videos[0]
            else:
                # Sort by mtime to get newest if multiple
                video_matches.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                video_path = video_matches[0]

            if not video_path or not os.path.exists(video_path):
                # Fallback: try to construct path from info
                ext = info.get("ext", "mp4")
                expected_path = os.path.join(DOWNLOADS_DIR, f"{video_id}.{ext}")
                if os.path.exists(expected_path):
                    video_path = expected_path
                else:
                    raise ValueError("Downloaded file not found after download")

            # Verify file size (extra check)
            try:
                file_size = os.path.getsize(video_path)
                if file_size > 100 * 1024 * 1024:
                    # Remove oversized file
                    try:
                        os.remove(video_path)
                    except:
                        pass
                    raise ValueError("Video too large (max 100MB)")
                if file_size == 0:
                    raise ValueError("Video unavailable - downloaded file is empty")
            except OSError:
                pass  # If we can't get size, continue

            return {
                "video_path": video_path,
                "original_caption": original_caption,
                "title": title,
                "duration": duration,
            }

    except ValueError:
        # Re-raise our custom ValueErrors
        raise
    except DownloadError as e:
        # Catch any remaining DownloadErrors
        error_msg = str(e).lower()
        if "too large" in error_msg or "max_filesize" in error_msg:
            raise ValueError("Video too large (max 100MB)")
        elif "private" in error_msg or "unavailable" in error_msg or "deleted" in error_msg:
            raise ValueError("Video unavailable")
        else:
            raise ValueError(f"Invalid or unsupported URL: {str(e)[:200]}")
    except Exception as e:
        # Catch-all for unexpected errors
        if isinstance(e, ValueError):
            raise
        raise ValueError(f"Download failed: {str(e)[:300]}")
