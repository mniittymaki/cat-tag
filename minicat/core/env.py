"""Environment and runtime detection helpers.

Especially useful for distinguishing development runs from PyInstaller bundles.
"""

from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """
    Return True if the application is running as a frozen PyInstaller bundle.
    This is the standard way to detect bundled apps.
    """
    return bool(getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None))


def get_app_base_dir() -> Path:
    """
    Return the base directory of the running application.

    - When frozen (PyInstaller .app): returns the directory containing the executable.
    - When running from source: returns the project root (two levels above this file).
    """
    if is_frozen():
        # sys.executable points to the binary inside the .app bundle
        return Path(sys.executable).parent
    else:
        # This file lives in minicat/core/env.py → go up to project root
        return Path(__file__).resolve().parents[2]


def get_resource_path(relative: str | Path) -> Path:
    """
    Get an absolute path to a resource that works both in development and when frozen.

    Example:
        icon_path = get_resource_path("assets/cat-tag.icns")
    """
    base = get_app_base_dir()
    return base / relative


def get_ffmpeg_install_hint() -> str:
    """
    Return a user-friendly install hint that adapts to whether we're bundled or not.
    """
    if is_frozen():
        return (
            "CAT+TAG needs ffmpeg for thumbnails, storyboards, and proxy generation.\n\n"
            "Please install it with:\n"
            "    brew install ffmpeg\n\n"
            "Then restart CAT+TAG."
        )
    else:
        return (
            "CAT+TAG needs ffmpeg for thumbnails, storyboards, and proxy generation.\n\n"
            "Please install it with:\n"
            "    brew install ffmpeg\n\n"
            "Then restart the app."
        )
