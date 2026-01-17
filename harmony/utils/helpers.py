"""Utility helper functions for Harmony."""

import re
from typing import Dict, Optional


def parse_title(title: str) -> Dict[str, str]:
    """Parse a title string to extract components."""
    if not title:
        return {"title": "", "artist": "", "album": ""}

    # Simple parser - can be enhanced
    parts = title.split(" - ")
    if len(parts) == 2:
        return {
            "artist": parts[0].strip(),
            "title": parts[1].strip(),
            "album": "",
        }
    return {"title": title, "artist": "", "album": ""}


def clean_album_name(album: str) -> str:
    """Clean album name for consistency."""
    if not album:
        return ""

    # Remove common suffixes
    album = re.sub(r'\s*\(.*?\)\s*$', '', album)
    album = re.sub(r'\s*\[.*?\]\s*$', '', album)
    return album.strip()


def format_track_info(title: str, artist: str, album: str = "") -> str:
    """Format track information as a readable string."""
    parts = []
    if artist:
        parts.append(artist)
    if title:
        parts.append(title)
    if album:
        parts.append(f"({album})")
    return " - ".join(parts) if parts else "Unknown"
