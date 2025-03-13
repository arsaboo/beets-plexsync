"""General utility functions for PlexSync plugin."""

import re
from difflib import SequenceMatcher


def clean_string(s):
    """Clean a string by removing special characters and converting to lowercase."""
    if not s:
        return ""
    # Remove special characters and convert to lowercase
    return re.sub(r'[^\w\s]', '', s.lower())


def get_fuzzy_score(str1, str2):
    """Get fuzzy matching score between two strings."""
    if not str1 or not str2:
        return 0.0
    return SequenceMatcher(None, str1, str2).ratio()


def clean_text_for_matching(text):
    """Clean text for better matching."""
    if not text:
        return ""
    # Convert to lowercase
    text = text.lower()
    # Remove special characters
    text = re.sub(r'[^\w\s]', ' ', text)
    # Remove extra whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    # Remove common words that don't help with matching
    stop_words = ['the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'with', 'by', 'from']
    words = text.split()
    words = [w for w in words if w not in stop_words]
    return ' '.join(words)


def calculate_string_similarity(str1, str2):
    """Calculate similarity between two strings."""
    if not str1 or not str2:
        return 0.0

    # Clean strings
    clean_str1 = clean_text_for_matching(str1)
    clean_str2 = clean_text_for_matching(str2)

    # Calculate similarity
    return get_fuzzy_score(clean_str1, clean_str2)


def calculate_artist_similarity(artist1, artist2):
    """Calculate similarity between artist names, handling special cases."""
    if not artist1 or not artist2:
        return 0.0

    # Clean strings
    clean_artist1 = clean_text_for_matching(artist1)
    clean_artist2 = clean_text_for_matching(artist2)

    # Handle "Various Artists" case
    if "various" in clean_artist1 or "various" in clean_artist2:
        if "various" in clean_artist1 and "various" in clean_artist2:
            return 1.0
        return 0.3  # Partial match for Various Artists

    # Handle artist name variations (e.g., "A & B" vs "A and B")
    artist1_normalized = re.sub(r'\s+&\s+', ' and ', clean_artist1)
    artist2_normalized = re.sub(r'\s+&\s+', ' and ', clean_artist2)

    # Calculate similarity
    return get_fuzzy_score(artist1_normalized, artist2_normalized)


def ensure_float(value, default=0.0):
    """Convert value to float, with default for None or errors."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def parse_title(title):
    """Parse title with soundtrack information.

    Args:
        title: Title string possibly containing soundtrack info

    Returns:
        tuple: (cleaned_title, soundtrack_album)
    """
    # Handle "From 'Movie'" pattern
    from_movie_match = re.search(r'(.*?)\s+(?:From|from)\s+["\'](.+?)["\']', title)
    if from_movie_match:
        title = from_movie_match.group(1).strip()
        album = from_movie_match.group(2).strip()
        return title, album

    # Handle "Title (From 'Movie')" pattern
    paren_match = re.search(r'(.*?)\s+\((?:From|from)\s+["\'](.+?)["\'].*?\)', title)
    if paren_match:
        title = paren_match.group(1).strip()
        album = paren_match.group(2).strip()
        return title, album

    # No soundtrack info found
    return title, None


def clean_album_name(album):
    """Clean album name by removing common suffixes."""
    if not album:
        return album

    # Remove common suffixes
    suffixes = [
        r'\s+\(Original Motion Picture Soundtrack\)',
        r'\s+\(Music from the Motion Picture\)',
        r'\s+\(Original Soundtrack\)',
        r'\s+\(Motion Picture Soundtrack\)',
        r'\s+\(From [^)]+\)',
        r'\s+\(Soundtrack\)',
        r'\s+Soundtrack',
        r'\s+OST'
    ]

    cleaned = album
    for suffix in suffixes:
        cleaned = re.sub(suffix, '', cleaned, flags=re.IGNORECASE)

    return cleaned.strip()


def clean_title(title):
    """Clean track title by removing common suffixes and prefixes."""
    if not title:
        return title

    # Remove common patterns
    patterns = [
        r'\s+\(From [^)]+\)',
        r'\s+\(feat\.[^)]+\)',
        r'\s+\(ft\.[^)]+\)',
        r'\s+\(with [^)]+\)',
        r'\s+\(Soundtrack Version\)',
        r'\s+\(Movie Version\)',
        r'\s+\(Original Mix\)',
        r'\s+\(Radio Edit\)',
        r'\s+\(Remastered\)',
        r'\s+\(Bonus Track\)',
        r'\s+\(Deluxe Edition\)',
        r'\s+\(Explicit\)',
        r'\s+\(Clean\)'
    ]

    cleaned = title
    for pattern in patterns:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)

    return cleaned.strip()


def get_color_for_score(score):
    """Return a color name based on match score."""
    if score >= 0.8:
        return 'text_success'  # Green for good matches
    elif score >= 0.5:
        return 'text_warning'  # Yellow for medium matches
    else:
        return 'text_error'    # Red for poor matches
