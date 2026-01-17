"""Custom matching utilities for Harmony."""

import re
import difflib
from typing import Optional, Tuple, Iterable, Dict, Any
from dataclasses import dataclass


@dataclass
class MatchScore:
    """Match score result with breakdown."""

    similarity: float
    confidence: float
    distance: float
    details: Dict[str, float]


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
    s = s.replace(""", '"').replace(""", '"').replace("'", "'")
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


def _tokenize_whole_query(query_dict: Dict[str, Optional[str]]) -> str:
    """Combine all query fields into a single normalized string for fuzzy matching.
    
    This handles cases where source metadata has incorrect field assignments
    (e.g., YouTube titles with embedded artist/album info like "Song | Artist | Album").
    
    Args:
        query_dict: Dict with 'title', 'artist', 'album' keys
        
    Returns:
        Single normalized string with all meaningful content
    """
    parts = []
    for field in ['title', 'artist', 'album']:
        value = query_dict.get(field, '') or ''
        value_str = str(value).strip()
        # Skip empty or placeholder values
        if value_str and value_str.lower() not in ['none', 'unknown', '', 'null']:
            cleaned = clean_string(value_str)
            if cleaned:
                parts.append(cleaned)
    return ' '.join(parts)


def extract_soundtrack_info(s: str) -> Tuple[str, str]:
    """Extract soundtrack information from a string with enhanced pattern detection.

    Returns a tuple of (main_title, soundtrack_title) where soundtrack_title
    is the extracted movie/album name if a pattern is found; otherwise empty.
    """
    if not s:
        return s, ""

    text = s.strip()

    # Enhanced patterns for soundtrack detection
    patterns = [
        # Song - From "Movie" (quotes optional)
        r"^(.*?)\s*[-–]\s*from\s+[\"\"\"]?(.+?)[\"\"\"]?$",
        # Song (From "Movie") or [From "Movie"] (quotes optional)
        r"^(.*?)\s*[\(\[]\s*from\s+[\"\"\"]?(.+?)[\"\"\"]?\s*[\)\]]",
        # Song (Soundtrack from "Movie")
        r"^(.*?)\s*[\(\[]\s*soundtrack\s+from\s+[\"\"\"]?(.+?)[\"\"\"]?\s*[\)\]]",
        # Song (Music from "Movie")
        r"^(.*?)\s*[\(\[]\s*music\s+from\s+[\"\"\"]?(.+?)[\"\"\"]?\s*[\)\]]",
        # Song (From the movie "Movie")
        r"^(.*?)\s*[\(\[]\s*from\s+the\s+movie\s+[\"\"\"]?(.+?)[\"\"\"]?\s*[\)\]]",
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

    return s, ""


def get_fuzzy_score(str1: Optional[str], str2: Optional[str]) -> float:
    """Return a basic fuzzy match score between two strings."""
    if not str1 or not str2:
        return 0.0
    return difflib.SequenceMatcher(None, str1.lower(), str2.lower()).ratio()


def clean_text_for_matching(text: Optional[str]) -> str:
    """Normalize text to improve fuzzy matching consistency."""
    if not text:
        return ""

    text = text.lower()
    text = re.sub(r'\([^)]*\)', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    text = re.sub(r'(?i)original\s+(?:motion\s+picture\s+)?soundtrack', '', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    return ' '.join(text.split())


def calculate_string_similarity(source: Optional[str], target: Optional[str]) -> float:
    """Compute similarity between two normalized strings."""
    if not source or not target:
        return 0.0

    source = source.lower().strip()
    target = target.lower().strip()
    if source == target:
        return 1.0
    if source in target or target in source:
        shorter = min(len(source), len(target))
        longer = max(len(source), len(target))
        return 0.9 * (shorter / longer)
    return difflib.SequenceMatcher(None, source, target).ratio()


def string_dist(source: str, target: str) -> float:
    """Calculate string distance (inverse of similarity)."""
    similarity = calculate_string_similarity(source, target)
    return 1.0 - similarity


def calculate_artist_similarity(
    source_artists: Optional[Iterable[str]], target_artists: Optional[Iterable[str]]
) -> float:
    """Compare two artist lists allowing for partial matches."""
    source = [a for a in (source_artists or []) if a]
    target = [a for a in (target_artists or []) if a]
    if not source or not target:
        return 0.0

    def normalize_artist(artist: str) -> str:
        artist = artist.lower()
        artist = re.sub(r'\s*[&,]\s*', ' and ', artist)
        artist = re.sub(r'\s*(?:feat\.?|ft\.?|featuring)\s*.*$', '', artist)
        artist = re.sub(r'[^\w\s]', '', artist)
        return artist.strip()

    def split_parts(artist: str) -> set[str]:
        return set(normalize_artist(artist).split())

    source_norm = []
    for artist in source:
        normalized = normalize_artist(artist)
        if normalized:
            source_norm.append(normalized)

    target_norm = []
    for artist in target:
        normalized = normalize_artist(artist)
        if normalized:
            target_norm.append(normalized)

    if not source_norm or not target_norm:
        return 0.0

    exact_matches = len(set(source_norm).intersection(target_norm))
    if exact_matches:
        return exact_matches / max(len(source_norm), len(target_norm))

    source_parts = set().union(*(split_parts(a) for a in source_norm))
    target_parts = set().union(*(split_parts(a) for a in target_norm))
    if not source_parts or not target_parts:
        return 0.0

    intersection = len(source_parts & target_parts)
    union = len(source_parts | target_parts)
    return 0.8 * (intersection / union if union else 0.0)


def calculate_field_weight(field_value: str, field_type: str) -> float:
    """Calculate dynamic weight for a field based on its quality and content."""
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
    """Assess the quality of a field value for confidence calculation."""
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
    def split_artists(s: str) -> Tuple[set[str], set[str]]:
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
        best_match = min(string_dist(artist1, artist2) for artist2 in all_artists2)
        matches.append(best_match)

    # Return average distance with a slight bonus for having more matching main artists
    avg_distance = sum(matches) / len(matches)

    # Bonus for matching main artists (more important than featured)
    main_matches = len(main_artists1.intersection(main_artists2))
    if main_matches > 0:
        avg_distance *= 0.9  # 10% bonus for main artist matches

    return avg_distance


def plex_track_distance(
    query: Dict[str, Optional[str]],
    plex_track: Any,
    config: Optional[dict] = None
) -> MatchScore:
    """Calculate distance between a search query and Plex Track with enhanced matching.

    Args:
        query: Dict with 'title', 'artist', 'album' keys
        plex_track: Plex Track object or dict with track fields
        config: Optional configuration dict

    Returns:
        MatchScore with similarity (0-1), confidence, and details
    """
    # Extract query fields
    query_title = query.get("title", "")
    query_artist = query.get("artist", "")
    query_album = query.get("album", "")

    # Extract track fields (handle both Plex objects and dicts)
    if isinstance(plex_track, dict):
        # Dictionary mode (from search pipeline)
        plex_title = plex_track.get("title", "")
        plex_artist = plex_track.get("artist", "")
        plex_album = plex_track.get("album", "")
    else:
        # Plex object mode
        plex_title = getattr(plex_track, "title", "")
        plex_artist = getattr(plex_track, "originalTitle", None) or (
            plex_track.artist().title if hasattr(plex_track, "artist") and callable(plex_track.artist) else ""
        )
        plex_album = getattr(plex_track, "parentTitle", "")

    # Check which fields are available
    has_title = bool(query_title and query_title.strip())
    has_artist = bool(query_artist and query_artist.strip())
    has_album = bool(query_album and query_album.strip())

    if not has_title and not has_artist and not has_album:
        return MatchScore(similarity=0.0, confidence=0.0, distance=1.0, details={})

    # Calculate field qualities for confidence scoring
    field_qualities = {}
    if has_title:
        field_qualities['title'] = assess_field_quality(query_title)
    if has_artist:
        field_qualities['artist'] = assess_field_quality(query_artist)
    if has_album:
        field_qualities['album'] = assess_field_quality(query_album)

    # Calculate dynamic weights based on field quality
    dynamic_weights = {}
    if has_title:
        dynamic_weights['title'] = calculate_field_weight(query_title, 'title')
    if has_artist:
        dynamic_weights['artist'] = calculate_field_weight(query_artist, 'artist')
    if has_album:
        dynamic_weights['album'] = calculate_field_weight(query_album, 'album')

    # Available fields
    available_fields = list(dynamic_weights.keys())

    # Normalize dynamic weights
    total_weight = sum(dynamic_weights.values())
    if total_weight > 0:
        weights = {
            field: dynamic_weights[field] / total_weight
            for field in available_fields
        }
    else:
        weights = {
            field: 1.0 / len(available_fields)
            for field in available_fields
        }

    # Calculate individual field distances
    details = {}

    # Album comparison
    if has_album:
        album1 = clean_string(query_album)
        album2 = clean_string(plex_album)

        album_dist = string_dist(album1, album2)

        # Enhanced soundtrack-aware logic
        main_album1, soundtrack_album1 = extract_soundtrack_info(query_album)
        main_album2, soundtrack_album2 = extract_soundtrack_info(plex_album)

        soundtrack_album1_cleaned = clean_string(soundtrack_album1)
        soundtrack_album2_cleaned = clean_string(soundtrack_album2)

        # If both have soundtrack titles, compare those
        if soundtrack_album1_cleaned and soundtrack_album2_cleaned:
            soundtrack_dist = string_dist(soundtrack_album1_cleaned, soundtrack_album2_cleaned)
            if soundtrack_dist < 0.3:
                main_album1_cleaned = clean_string(main_album1)
                main_album2_cleaned = clean_string(main_album2)
                album_dist = string_dist(main_album1_cleaned, main_album2_cleaned)
                album_dist = max(0.0, album_dist - 0.4)
            else:
                album_dist = min(1.0, album_dist + 0.2)
        elif soundtrack_album1_cleaned:
            if soundtrack_album1_cleaned == album2:
                album_dist = max(0.0, album_dist - 0.3)
        elif soundtrack_album2_cleaned:
            if soundtrack_album2_cleaned == album1:
                album_dist = max(0.0, album_dist - 0.3)

        # Check if album is contained within the other
        if album1 in album2 or album2 in album1:
            album_dist = max(0.0, album_dist - 0.3)

        details['album'] = 1.0 - album_dist

    # Title comparison
    if has_title:
        title1 = clean_string(query_title)
        title2 = clean_string(plex_title)

        main_title1, soundtrack_title1 = extract_soundtrack_info(query_title)
        main_title2, soundtrack_title2 = extract_soundtrack_info(plex_title)

        soundtrack_title1_cleaned = clean_string(soundtrack_title1)
        soundtrack_title2_cleaned = clean_string(soundtrack_title2)

        if soundtrack_title1_cleaned and soundtrack_title2_cleaned:
            soundtrack_dist = string_dist(soundtrack_title1_cleaned, soundtrack_title2_cleaned)
            if soundtrack_dist < 0.3:
                main_title1_cleaned = clean_string(main_title1)
                main_title2_cleaned = clean_string(main_title2)
                title_dist = string_dist(main_title1_cleaned, main_title2_cleaned)
                title_dist = max(0.0, title_dist - 0.4)
            else:
                title_dist = string_dist(title1, title2)
                title_dist = min(1.0, title_dist + 0.2)
        elif title1 in title2 or title2 in title1:
            title_dist = string_dist(title1, title2)
            title_dist = max(0.0, title_dist - 0.2)
        else:
            title_dist = string_dist(title1, title2)

        details['title'] = 1.0 - title_dist

    # Artist comparison
    if has_artist:
        artist_dist = enhanced_artist_distance(query_artist, plex_artist)
        details['artist'] = 1.0 - artist_dist

    # Calculate weighted distance (field-by-field approach)
    total_distance = 0.0
    for field in available_fields:
        field_similarity = details.get(field, 0.0)
        field_distance = 1.0 - field_similarity
        total_distance += weights[field] * field_distance

    # Convert to similarity score
    field_similarity_score = 1.0 - total_distance
    
    # FALLBACK: Whole-query fuzzy matching for misaligned fields
    # This handles cases where source metadata has incorrect field assignments
    # (e.g., YouTube titles like "Song | Artists | Album" parsed incorrectly)
    query_combined = _tokenize_whole_query(query)
    track_combined = _tokenize_whole_query({
        'title': plex_title,
        'artist': plex_artist,
        'album': plex_album
    })
    
    # Calculate whole-query similarity
    if query_combined and track_combined:
        whole_query_dist = string_dist(query_combined, track_combined)
        whole_query_similarity = 1.0 - whole_query_dist
    else:
        whole_query_similarity = 0.0
    
    # Use the better score, with preference for field-by-field (0.85 factor)
    # This ensures structured queries still work best, but broken queries have a fallback
    raw_score = max(field_similarity_score, whole_query_similarity * 0.85)
    
    # Store both scores in details for debugging
    details['field_similarity'] = field_similarity_score
    details['whole_query_similarity'] = whole_query_similarity

    # Calculate confidence multiplier based on available fields and their qualities
    if available_fields:
        base_confidence = len(available_fields) / 3.0
        if field_qualities:
            avg_quality = sum(field_qualities[field] for field in available_fields) / len(available_fields)
        else:
            avg_quality = 0.5
        confidence = base_confidence * (0.5 + 0.5 * avg_quality)
        
        # Apply confidence dampening strategically
        # If whole-query won (scored higher after 0.85 factor), skip additional dampening
        # The 0.85 factor already serves as a confidence penalty for field-misaligned queries
        if raw_score > field_similarity_score:
            # Whole-query won - already penalized by 0.85 factor, no additional dampening
            score = raw_score
        else:
            # Field-by-field won - apply normal confidence dampening
            score = max(raw_score * confidence, raw_score * 0.5)
    else:
        score = raw_score
        confidence = 0.5

    # Ensure exact/near-exact matches surface with high confidence
    if raw_score >= 0.99:
        score = max(score, 0.95)

    return MatchScore(
        similarity=score,
        confidence=confidence,
        distance=total_distance,
        details=details
    )
