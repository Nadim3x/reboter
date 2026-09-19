"""
caption.py — Caption Selector & Config Helpers

Handles caption selection logic:
- Uses original caption if available
- Falls back to random caption from pool
- Provides atomic config load/save helpers used across the project
"""

import json
import os
import random
import tempfile
from typing import Dict, Any


# Default config path relative to this file's directory
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")


def load_config(config_path: str = CONFIG_PATH) -> Dict[str, Any]:
    """
    Load config.json and return as dict.

    Args:
        config_path: Path to config.json file

    Returns:
        dict: Parsed config data

    Raises:
        FileNotFoundError: If config file doesn't exist
        json.JSONDecodeError: If config is invalid JSON
    """
    # Validate file exists
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Config file not found at {config_path}. "
            "Copy config.example.json to config.json and fill in your tokens."
        )

    # Read and parse JSON
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {config_path}: {e}")
    except Exception as e:
        raise IOError(f"Failed to read config {config_path}: {e}")


def save_config(data: Dict[str, Any], config_path: str = CONFIG_PATH) -> None:
    """
    Save config dict back to config.json atomically.

    Uses a temporary file + os.replace to avoid partial writes
    if the process crashes mid-write.

    Args:
        data: Config dict to save
        config_path: Path to config.json file
    """
    # Ensure directory exists
    config_dir = os.path.dirname(os.path.abspath(config_path))
    os.makedirs(config_dir, exist_ok=True)

    # Write to temp file first, then atomically replace
    try:
        # Create temp file in same directory for atomic rename
        fd, temp_path = tempfile.mkstemp(
            dir=config_dir, prefix=".config_tmp_", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(data, tmp_file, indent=2, ensure_ascii=False)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            # Atomic replace
            os.replace(temp_path, config_path)
        except Exception:
            # Cleanup temp file on failure
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
    except Exception as e:
        raise IOError(f"Failed to save config to {config_path}: {e}")


def get_caption(original_caption: str | None) -> str:
    """
    Select caption to use for posting.

    Logic:
    1. If original_caption is not None and not empty -> use it (truncated to 2200 chars)
    2. Else load captions pool from config.json and pick random
    3. If pool empty -> return default fallback

    Args:
        original_caption: Caption/description extracted from video source, or None

    Returns:
        str: Final caption to use
    """
    # Case 1: Use original caption if available
    if original_caption is not None and isinstance(original_caption, str):
        cleaned = original_caption.strip()
        if cleaned != "":
            # Instagram caption limit is 2200 chars
            if len(cleaned) > 2200:
                cleaned = cleaned[:2197] + "..."
            return cleaned

    # Case 2: Fallback to random caption from pool
    try:
        config = load_config()
        captions_pool = config.get("captions", [])

        # Ensure it's a list and not empty
        if isinstance(captions_pool, list) and len(captions_pool) > 0:
            # Filter out empty strings
            valid_captions = [c.strip() for c in captions_pool if isinstance(c, str) and c.strip() != ""]
            if valid_captions:
                return random.choice(valid_captions)

    except FileNotFoundError:
        # Config missing, will fallback to default
        pass
    except Exception:
        # Any config read error, fallback to default
        pass

    # Case 3: Ultimate fallback
    return "Check this out! 🔥"
