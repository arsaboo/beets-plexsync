"""Custom matching utilities for PlexSync plugin."""

import re
from typing import Optional, Tuple

from beets.autotag import hooks
from beets.library import Item
from plexapi.audio import Track


def clean_string(s: str) -> str:
    """Clean a string for comparison by removing common variations."""
    if not s:
        return ""

    s = s.lower()

    # Remove common prefixes/suffixes
    s = re.sub(r"^the\s+", "", s)
    s = re.sub(r"\s*\([^)]*\)", "", s)  # Remove parentheses and contents
    s = re.sub(r"\s*\[[^\]]*\]", "", s)  # Remove brackets and contents
    s = re.sub(r"\s*feat\.?\s.*$", "", s, flags=re.IGNORECASE)  # Remove featuring
    s = re.sub(r"\s*ft\.?\s.*$", "", s, flags=re.IGNORECASE)  # Remove ft.
    s = re.sub(r"\s*with\s.*$", "", s, flags=re.IGNORECASE)  # Remove with...

    # Normalize separators
    s = re.sub(r"[&,/\\]", " and ", s)
    s = re.sub(r"\s+", " ", s)  # Normalize whitespace

    return s.strip()


def artist_distance(str1: str, str2: str) -> float:
    """Calculate artist name distance with special handling for multiple artists."""
    if not str1 or not str2:
        return 1.0

    # Split artists on common separators
    def split_artists(s):
        return {clean_string(a) for a in re.split(r"[,;&/]|\s+and\s+|\s+&\s+", s) if a}

    artists1 = split_artists(str1)
    artists2 = split_artists(str2)

    if not artists1 or not artists2:
        return 1.0

    # Calculate best match for each artist using hooks.string_dist
    matches = []
    for artist1 in artists1:
        best_match = min(hooks.string_dist(artist1, artist2) for artist2 in artists2)
        matches.append(best_match)

    return sum(matches) / len(matches)


def plex_track_distance(
    item: Item,
    plex_track: Track,
    config: Optional[dict] = None
) -> Tuple[float, hooks.Distance]:
    """Calculate distance between a beets Item and Plex Track."""
    # Define weights
    weights = {
        'title': 0.45,    # Title most important
        'artist': 0.35,   # Artist next
        'album': 0.20,    # Album title
    }

    # Create distance object with initial weights
    dist = hooks.Distance()
    dist._weights.update(weights)

    # Title comparison
    title1 = clean_string(item.title)
    title2 = clean_string(plex_track.title)
    dist.add_string('title', title1, title2)

    # Artist comparison (using our custom artist_distance)
    artist1 = item.artist
    artist2 = plex_track.originalTitle or plex_track.artist().title
    dist.add_ratio('artist', artist_distance(artist1, artist2), 1.0)

    # Album comparison
    album1 = clean_string(item.album)
    album2 = clean_string(plex_track.parentTitle)
    dist.add_string('album', album1, album2)

    # Get total distance - it's a property, not a method
    total_dist = dist.distance

    # Convert to similarity score where 1 is perfect match
    score = 1 - total_dist

    return score, dist

