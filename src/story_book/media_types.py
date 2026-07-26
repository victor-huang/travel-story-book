"""Which files count as media.

Shared so the scanner (T10) and the profiler (T17) cannot disagree about what is importable.
Import from here rather than redefining an extension list.
"""

from __future__ import annotations

from pathlib import Path

from story_book.db.models import MediaKind

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".dng", ".webp"}
)
VIDEO_EXTENSIONS = frozenset({".mov", ".mp4", ".m4v", ".avi"})
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# macOS and Windows metadata droppings, plus editor sidecars. Never media, always present.
IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})
IGNORED_EXTENSIONS = frozenset({".aae", ".xmp", ".thm", ".lrv"})


def classify(path: Path) -> MediaKind | None:
    """The media kind for a path, or None if it is not importable media."""
    suffix = path.suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return MediaKind.IMAGE
    if suffix in VIDEO_EXTENSIONS:
        return MediaKind.VIDEO
    return None


def is_hidden(path: Path) -> bool:
    """Dotfiles and anything inside a dot-directory."""
    return any(part.startswith(".") for part in path.parts)
