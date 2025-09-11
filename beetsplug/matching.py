"""Custom matching utilities for PlexSync plugin."""

import re
from typing import Optional, Tuple

from beets.autotag import hooks
try:
    from beets.autotag.hooks import Distance
except ImportError:
    from beets.autotag.distance import Distance
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

    # Add handling for year variations
    s = re.sub(r"\s*\d{4}\s*$", "", s)  # Remove year at end

    return s.strip()


def extract_soundtrack_info(s: str) -> tuple[str, str]:
    """Extract soundtrack information from a string.
    
    Returns a tuple of (cleaned_string, soundtrack_title) where soundtrack_title
    is the extracted movie/album name if pattern is found, otherwise empty string.
    """
    if not s:
        return s, ""
    
    # Look for patterns like 'Sunn Mere Yaar Ve - From "Param Sundari"'
    soundtrack_pattern = re.compile(r'(.*)\s*[-â€“]\s*from\s*"([^"]+)"', re.IGNORECASE)
    match = soundtrack_pattern.search(s)
    
    if match:
        # Extract the main title and soundtrack title
        main_title = match.group(1).strip()
        soundtrack_title = match.group(2).strip()
        return main_title, soundtrack_title
    
    # Also handle the pattern like '(From "...")' or '[From "..."]'
    paren_pattern = re.compile(r'(.*)\s*[([]from\s*"([^"]+)"[)\]]', re.IGNORECASE)
    match = paren_pattern.search(s)
    
    if match:
        main_title = match.group(1).strip()
        soundtrack_title = match.group(2).strip()
        return main_title, soundtrack_title
    
    # No pattern found, return original string with empty soundtrack title
    return s, ""


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
) -> Tuple[float, Distance]:
    """Calculate distance between a beets Item and Plex Track."""
    # Define base weights that will be adjusted based on available fields
    base_weights = {
        'title': 0.40,    # Title important, but slightly reduced
        'artist': 0.30,   # Artist next
        'album': 0.30,    # Album title - increased to match importance
    }

    # Create distance object
    dist = Distance()

    # Check which fields are available
    has_title = bool(item.title and item.title.strip())
    has_artist = bool(item.artist and item.artist.strip())
    has_album = bool(item.album and item.album.strip())

    # Calculate actual weights based on available fields
    available_fields = []
    if has_title:
        available_fields.append('title')
    if has_artist:
        available_fields.append('artist')
    if has_album:
        available_fields.append('album')

    if not available_fields:
        return 0.0, dist  # No fields to compare

    # Redistribute weights
    total_base_weight = sum(base_weights[f] for f in available_fields)
    weights = {
        field: base_weights[field] / total_base_weight
        for field in available_fields
    }

    dist._weights.update(weights)

    # Album comparison first (if available)
    if has_album:
        album1 = clean_string(item.album)
        album2 = clean_string(plex_track.parentTitle)

        # Use string_dist for album but normalize properly
        album_dist = hooks.string_dist(album1, album2)

        # --- START OF NEW SOUNDTRACK-AWARE LOGIC ---
        # Check for soundtrack patterns in the original album title
        soundtrack_pattern = re.compile(r'\(from "([^"]+)"\)', re.IGNORECASE)
        match = soundtrack_pattern.search(item.album)

        if match:
            # If pattern found, extract the movie title
            extracted_album_title = match.group(1).lower().strip()
            plex_album_title_cleaned = clean_string(plex_track.parentTitle).lower()

            # If extracted title matches the Plex album, apply a large bonus
            if extracted_album_title == plex_album_title_cleaned:
                album_dist = album_dist * 0.1  # Reduce distance by 90%
        # --- END OF NEW SOUNDTRACK-AWARE LOGIC ---

        # Check if album is contained within the other (for partial matches like "Andaz" in "Andaaz (1971)")
        if album1 in album2 or album2 in album1:
            # Apply a bonus for partial containment
            album_dist = max(0.0, album_dist - 0.3)

        dist.add_ratio('album', album_dist, 1.0)

        # If we only have album and it's a perfect match, return perfect score
        if len(available_fields) == 1 and album_dist == 0:
            return 1.0, dist

    # Title comparison (if available)
    if has_title:
        title1 = clean_string(item.title)
        title2 = clean_string(plex_track.title)

        # --- NEW SOUNDTRACK-AWARE LOGIC FOR TITLE ---
        # Extract soundtrack info from titles
        main_title1, soundtrack_title1 = extract_soundtrack_info(item.title)
        main_title2, soundtrack_title2 = extract_soundtrack_info(plex_track.title)
        
        # Clean the extracted soundtrack titles
        soundtrack_title1_cleaned = clean_string(soundtrack_title1)
        soundtrack_title2_cleaned = clean_string(soundtrack_title2)
        
        # If both have soundtrack titles, compare those
        if soundtrack_title1_cleaned and soundtrack_title2_cleaned:
            # Compare the soundtrack titles
            soundtrack_dist = hooks.string_dist(soundtrack_title1_cleaned, soundtrack_title2_cleaned)
            
            # Apply a bonus if they match
            if soundtrack_dist < 0.3:  # If soundtrack titles are similar
                # Use the main titles for comparison but with a bonus
                main_title1_cleaned = clean_string(main_title1)
                main_title2_cleaned = clean_string(main_title2)
                title_dist = hooks.string_dist(main_title1_cleaned, main_title2_cleaned)
                
                # Apply a bonus for matching soundtrack context
                title_dist = max(0.0, title_dist - 0.3)
                dist.add_ratio('title', title_dist, 1.0)
            else:
                # Fallback to standard title comparison
                dist.add_string('title', title1, title2)
        # If only one has a soundtrack title, check if it matches the other's album
        elif soundtrack_title1_cleaned and has_album:
            album2_cleaned = clean_string(plex_track.parentTitle)
            if soundtrack_title1_cleaned == album2_cleaned:
                # Apply bonus for matching soundtrack context
                main_title1_cleaned = clean_string(main_title1)
                title2_cleaned = clean_string(plex_track.title)
                title_dist = hooks.string_dist(main_title1_cleaned, title2_cleaned)
                title_dist = max(0.0, title_dist - 0.4)
                dist.add_ratio('title', title_dist, 1.0)
            else:
                # Fallback to standard title comparison
                dist.add_string('title', title1, title2)
        elif soundtrack_title2_cleaned and has_album:
            album1_cleaned = clean_string(item.album)
            if soundtrack_title2_cleaned == album1_cleaned:
                # Apply bonus for matching soundtrack context
                title1_cleaned = clean_string(item.title)
                main_title2_cleaned = clean_string(main_title2)
                title_dist = hooks.string_dist(title1_cleaned, main_title2_cleaned)
                title_dist = max(0.0, title_dist - 0.4)
                dist.add_ratio('title', title_dist, 1.0)
            else:
                # Fallback to standard title comparison
                dist.add_string('title', title1, title2)
        # --- END OF NEW SOUNDTRACK-AWARE LOGIC FOR TITLE ---
        else:
            # Check if one title contains the other (for partial matches)
            if (title1 and title2) and (title1 in title2 or title2 in title1):
                # Calculate string distance
                title_dist = hooks.string_dist(title1, title2)

                # Apply a bonus for partial containment
                title_dist = max(0.0, title_dist - 0.2)

                dist.add_ratio('title', title_dist, 1.0)
            else:
                # Use standard string comparison
                dist.add_string('title', title1, title2)

    # Artist comparison (if available)
    if has_artist:
        artist1 = item.artist
        artist2 = plex_track.originalTitle or plex_track.artist().title
        dist.add_ratio('artist', artist_distance(artist1, artist2), 1.0)

    # Get total distance
    total_dist = dist.distance

    # Convert to similarity score where 1 is perfect match
    score = 1 - total_dist

    return score, dist

