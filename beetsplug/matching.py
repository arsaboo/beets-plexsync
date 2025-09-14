"""Custom matching utilities for PlexSync plugin."""

import re
from typing import Optional, Tuple

from beets.autotag import hooks
from beets.autotag.distance import Distance, string_dist
from beets.library import Item
from plexapi.audio import Track


def clean_string(s: str) -> str:
    """Clean a string for comparison by removing common variations.

    Normalization pipeline:
    - lowercase and trim
    - strip surrounding quotes and normalize apostrophes
    - drop leading article "the"
    - remove parenthetical/bracketed segments anywhere
    - remove trailing featuring clauses (feat./ft./featuring/with ...)
    - drop common edition/suffix markers (remaster, radio edit, deluxe, etc.)
    - normalize separators (&, /, \) to spaces and collapse whitespace
    - remove a trailing year token
    """
    if not s:
        return ""

    s = s.strip().lower()

    # Strip quotes/apostrophes that are often cosmetic
    s = s.replace("“", '"').replace("”", '"').replace("’", "'")
    s = s.replace('"', "").replace("'", "")

    # Remove leading article
    s = re.sub(r"^\s*the\s+", "", s)

    # Remove parenthetical/bracketed segments (e.g., (Remastered 2011), [Live])
    s = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", s)

    # Remove featuring clauses at the end (feat./ft./featuring/with ...)
    s = re.sub(r"\s*(?:feat\.?|ft\.?|featuring|with)\s+.*$", "", s, flags=re.IGNORECASE)

    # Drop common edition/suffix markers if present after a dash
    s = re.sub(
        r"\s*-\s*(?:remaster(?:ed)?(?:\s+\d{4})?|radio edit|single version|album version|deluxe edition|expanded edition|clean version|explicit version)\b.*$",
        "",
        s,
        flags=re.IGNORECASE,
    )

    # Normalize separators
    s = re.sub(r"[&,/\\]", " ", s)
    s = re.sub(r"\s+", " ", s)  # Normalize whitespace

    # Remove trailing year token
    s = re.sub(r"\s*\b\d{4}\b\s*$", "", s)

    return s.strip()


def extract_soundtrack_info(s: str) -> tuple[str, str]:
    """Extract soundtrack information from a string with enhanced pattern detection.

    Returns a tuple of (main_title, soundtrack_title) where soundtrack_title
    is the extracted movie/album name if a pattern is found; otherwise empty.
    Patterns handled:
      - Song - From "Movie" / Song – From "Movie"
      - Song (From "Movie") / [From "Movie"] (quotes optional)
      - Song (Soundtrack from "Movie") / Song (Music from "Movie")
      - Song (From the movie "Movie")
      - Also supports the above without quotes around Movie
    """
    if not s:
        return s, ""

    # Use raw string for detection (don't pre-clean away quotes)
    text = s.strip()

    # Enhanced patterns for soundtrack detection
    patterns = [
        # Song - From "Movie" (quotes optional)
        r"^(.*?)\s*[-–]\s*from\s+[\"\“\”]?(.+?)[\"\”\“]?$",
        # Song (From "Movie") or [From "Movie"] (quotes optional)
        r"^(.*?)\s*[\(\[]\s*from\s+[\"\“\”]?(.+?)[\"\”\“]?\s*[\)\]]",
        # Song (Soundtrack from "Movie")
        r"^(.*?)\s*[\(\[]\s*soundtrack\s+from\s+[\"\“\”]?(.+?)[\"\”\“]?\s*[\)\]]",
        # Song (Music from "Movie")
        r"^(.*?)\s*[\(\[]\s*music\s+from\s+[\"\“\”]?(.+?)[\"\”\“]?\s*[\)\]]",
        # Song (From the movie "Movie")
        r"^(.*?)\s*[\(\[]\s*from\s+the\s+movie\s+[\"\“\”]?(.+?)[\"\”\“]?\s*[\)\]]",
        # Song - From Movie (no quotes)
        r"^(.*?)\s*[-–]\s*from\s+(.+)$",
        # Song (From Movie) (no quotes)
        r"^(.*?)\s*[\(\[]\s*from\s+(.+?)\s*[\)\]]",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            main_title = match.group(1).strip()
            soundtrack_title = match.group(2).strip().strip('"\"').strip()
            return main_title, soundtrack_title

    # No pattern found, return original string with empty soundtrack title
    return s, ""


def calculate_field_weight(field_value: str, field_type: str) -> float:
    """Calculate dynamic weight for a field based on its quality and content.

    Args:
        field_value: The value of the field
        field_type: Type of field ('title', 'artist', 'album')

    Returns:
        Weight value between 0.0 and 1.0
    """
    if not field_value or not field_value.strip():
        return 0.0

    field_value = field_value.strip()

    # Base weights by field type
    base_weights = {'title': 0.45, 'artist': 0.35, 'album': 0.20}
    weight = base_weights.get(field_type, 0.33)

    # Adjust based on text length (more words = more information)
    word_count = len(field_value.split())
    if word_count > 5:
        weight *= 1.2  # Boost for longer, more descriptive fields
    elif word_count < 2:
        weight *= 0.8  # Reduce for very short fields

    # Adjust for distinguishing features
    if re.search(r'\b\d{4}\b', field_value):  # Contains year as whole word
        weight *= 1.1
    if re.search(r'[([]+(?:feat|ft|with)[.)]| featuring', field_value, re.IGNORECASE):
        weight *= 1.05  # Slight boost for detailed artist info

    # Ensure weight doesn't exceed reasonable bounds
    return min(weight, 0.9)


def assess_field_quality(field_value: str) -> float:
    """Assess the quality of a field value for confidence calculation.

    Args:
        field_value: The value of the field

    Returns:
        Quality score between 0.0 (poor) and 1.0 (excellent)
    """
    if not field_value or not field_value.strip():
        return 0.0

    field_value = field_value.strip()

    # Start with a base quality score
    quality = 0.5

    # Increase quality for longer content (more information)
    word_count = len(field_value.split())
    if word_count > 10:
        quality += 0.3
    elif word_count > 5:
        quality += 0.2
    elif word_count > 2:
        quality += 0.1

    # Increase quality for presence of distinguishing features
    if re.search(r'\b\d{4}\b', field_value):  # Contains year
        quality += 0.1
    if '"' in field_value or "'" in field_value:  # Contains quoted text
        quality += 0.1

    # Decrease quality for generic terms
    generic_terms = ['unknown', 'track', 'song', 'untitled']
    if any(term in field_value.lower() for term in generic_terms):
        quality -= 0.2

    # Clamp to 0.0-1.0 range
    return max(0.0, min(1.0, quality))


def enhanced_artist_distance(str1: str, str2: str) -> float:
    """Calculate artist name distance with enhanced multiple artist handling."""
    if not str1 or not str2:
        return 1.0

    # Split artists on common separators with featured artist handling
    def split_artists(s):
        # Handle featured artists separately
        main_artist = re.sub(r'\s*(feat\.?|ft\.?|with)\s.*$', '', s, flags=re.IGNORECASE)
        featured_artists = re.findall(r'(?:feat\.?|ft\.?|with)\s+([^,;&/]+)', s, re.IGNORECASE)

        # Split main artist and add featured artists
        main_artists = {clean_string(a) for a in re.split(r'[,;&/]|\s+and\s+|\s+&\s+', main_artist) if a}
        featured_artists_cleaned = {clean_string(a) for a in featured_artists if a}

        return main_artists, featured_artists_cleaned

    main_artists1, feat_artists1 = split_artists(str1)
    main_artists2, feat_artists2 = split_artists(str2)

    all_artists1 = main_artists1.union(feat_artists1)
    all_artists2 = main_artists2.union(feat_artists2)

    if not all_artists1 or not all_artists2:
        return 1.0

    # Calculate matches with different weights for main vs featured artists
    matches = []
    for artist1 in all_artists1:
        best_match = min(hooks.string_dist(artist1, artist2) for artist2 in all_artists2)
        matches.append(best_match)

    # Return average distance with a slight bonus for having more matching main artists
    avg_distance = sum(matches) / len(matches)

    # Bonus for matching main artists (more important than featured)
    main_matches = len(main_artists1.intersection(main_artists2))
    if main_matches > 0:
        avg_distance *= 0.9  # 10% bonus for main artist matches

    return avg_distance


def plex_track_distance(
    item: Item,
    plex_track: Track,
    config: Optional[dict] = None
) -> Tuple[float, Distance]:
    """Calculate distance between a beets Item and Plex Track with enhanced matching."""
    # Create distance object
    dist = Distance()

    # Check which fields are available
    has_title = bool(item.title and item.title.strip())
    has_artist = bool(item.artist and item.artist.strip())
    has_album = bool(item.album and item.album.strip())

    # Calculate field qualities for confidence scoring
    field_qualities = {}
    if has_title:
        field_qualities['title'] = assess_field_quality(item.title)
    if has_artist:
        field_qualities['artist'] = assess_field_quality(item.artist)
    if has_album:
        field_qualities['album'] = assess_field_quality(item.album)

    # Calculate dynamic weights based on field quality
    dynamic_weights = {}
    if has_title:
        dynamic_weights['title'] = calculate_field_weight(item.title, 'title')
    if has_artist:
        dynamic_weights['artist'] = calculate_field_weight(item.artist, 'artist')
    if has_album:
        dynamic_weights['album'] = calculate_field_weight(item.album, 'album')

    # Available fields
    available_fields = list(dynamic_weights.keys())

    if not available_fields:
        return 0.0, dist  # No fields to compare

    # Normalize dynamic weights
    total_weight = sum(dynamic_weights.values())
    if total_weight > 0:
        weights = {
            field: dynamic_weights[field] / total_weight
            for field in available_fields
        }
    else:
        # Fallback to equal weights if all weights are zero
        weights = {
            field: 1.0 / len(available_fields)
            for field in available_fields
        }

    dist._weights.update(weights)

    # Album comparison (if available in search query)
    if has_album:
        album1 = clean_string(item.album)
        album2 = clean_string(plex_track.parentTitle)

        # Use string_dist for album but normalize properly
        album_dist = string_dist(album1, album2)

        # Enhanced soundtrack-aware logic
        # Extract soundtrack info from albums
        main_album1, soundtrack_album1 = extract_soundtrack_info(item.album)
        main_album2, soundtrack_album2 = extract_soundtrack_info(plex_track.parentTitle)

        # Clean the extracted soundtrack titles
        soundtrack_album1_cleaned = clean_string(soundtrack_album1)
        soundtrack_album2_cleaned = clean_string(soundtrack_album2)

        # If both have soundtrack titles, compare those
        if soundtrack_album1_cleaned and soundtrack_album2_cleaned:
            # Compare the soundtrack titles
            soundtrack_dist = hooks.string_dist(soundtrack_album1_cleaned, soundtrack_album2_cleaned)

            # Apply a bonus if they match
            if soundtrack_dist < 0.3:  # If soundtrack titles are similar
                # Use the main titles for comparison but with a bonus
                main_album1_cleaned = clean_string(main_album1)
                main_album2_cleaned = clean_string(main_album2)
                album_dist = hooks.string_dist(main_album1_cleaned, main_album2_cleaned)

                # Apply a bonus for matching soundtrack context
                album_dist = max(0.0, album_dist - 0.4)
            else:
                # Apply a small penalty for mismatched soundtracks
                album_dist = min(1.0, album_dist + 0.2)
        # If only one has a soundtrack title, check if it matches the other's album
        elif soundtrack_album1_cleaned:
            if soundtrack_album1_cleaned == album2:
                # Apply bonus for matching soundtrack context
                album_dist = max(0.0, album_dist - 0.3)
        elif soundtrack_album2_cleaned:
            if soundtrack_album2_cleaned == album1:
                # Apply bonus for matching soundtrack context
                album_dist = max(0.0, album_dist - 0.3)

        # Check if album is contained within the other (for partial matches)
        if album1 in album2 or album2 in album1:
            # Apply a bonus for partial containment
            album_dist = max(0.0, album_dist - 0.3)

        dist.add_ratio('album', album_dist, 1.0)

        # If we only have album and it's a perfect match, return high score
        if len(available_fields) == 1 and album_dist < 0.1:
            return 0.95, dist
    # Enhanced logic: Handle case where search query has no album but title contains soundtrack info
    elif has_title and not has_album:
        # Extract soundtrack info from the title
        main_title1, soundtrack_title1 = extract_soundtrack_info(item.title)
        soundtrack_title1_cleaned = clean_string(soundtrack_title1)

        # If we extracted soundtrack info from the title, check if it matches the Plex album
        if soundtrack_title1_cleaned:
            album2_cleaned = clean_string(plex_track.parentTitle)
            if soundtrack_title1_cleaned == album2_cleaned:
                # Provide a bonus for album validation (even though search has no album field)
                # This helps validate that this is a good match by confirming soundtrack context
                album_bonus_dist = 0.2  # Low distance to represent album validation
                dist.add_ratio('album', album_bonus_dist, 1.0)

    # Title comparison (if available)
    if has_title:
        title1 = clean_string(item.title)
        title2 = clean_string(plex_track.title)

        # Enhanced soundtrack-aware logic for title
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
                title_dist = max(0.0, title_dist - 0.4)
                dist.add_ratio('title', title_dist, 1.0)
            else:
                # Apply a small penalty for mismatched soundtracks
                title_dist = hooks.string_dist(title1, title2)
                title_dist = min(1.0, title_dist + 0.2)
                dist.add_ratio('title', title_dist, 1.0)
        # If only one has a soundtrack title, check if it matches related fields
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
        # Enhanced logic: Handle case where soundtrack info is in title but no album field in search query
        elif soundtrack_title1_cleaned and not has_album:
            # Check if the extracted soundtrack title matches the Plex track's album
            album2_cleaned = clean_string(plex_track.parentTitle)
            if soundtrack_title1_cleaned == album2_cleaned:
                # Apply bonus for matching soundtrack context
                main_title1_cleaned = clean_string(main_title1)
                title2_cleaned = clean_string(plex_track.title)
                title_dist = hooks.string_dist(main_title1_cleaned, title2_cleaned)
                title_dist = max(0.0, title_dist - 0.4)
                dist.add_ratio('title', title_dist, 1.0)

                # ALSO provide a bonus for the album comparison (even though search has no album)
                # This helps validate that this is a good match by confirming soundtrack context
                album_bonus_dist = 0.3  # Artificial low distance to represent album validation
                dist.add_ratio('album', album_bonus_dist, 1.0)
            else:
                # Fallback to standard title comparison
                dist.add_string('title', title1, title2)
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
        dist.add_ratio('artist', enhanced_artist_distance(artist1, artist2), 1.0)

    # Get total distance
    total_dist = dist.distance

    # Convert to similarity score where 1 is perfect match
    raw_score = 1 - total_dist

    # Calculate confidence multiplier based on available fields and their qualities
    if available_fields:
        # Base confidence based on number of fields
        base_confidence = len(available_fields) / 3.0  # Max 3 fields (title, artist, album)

        # Adjust based on field qualities (0.0 to 1.0 for each field)
        if field_qualities:
            avg_quality = sum(field_qualities[field] for field in available_fields) / len(available_fields)
        else:
            avg_quality = 0.5

        # Combine base confidence with quality adjustment
        confidence = base_confidence * (0.5 + 0.5 * avg_quality)  # Range from 0.5 to 1.0 of base

        # Apply confidence multiplier, but ensure we don't overly penalize
        score = max(raw_score * confidence, raw_score * 0.5)
    else:
        score = raw_score

    # Ensure exact/near-exact matches surface with high confidence in UI.
    # When all fields align and raw_score is ~1.0, the quality-based multiplier
    # can cap scores around 0.75 for short metadata. Bump such cases.
    if raw_score >= 0.99:
        score = max(score, 0.95)

    return score, dist
